"""
Namecheap Domain Automation Module
Handles domain availability checking, registration, and DNS TXT record management
via the Namecheap API for the Google Play Developer pipeline.

API Docs: https://www.namecheap.com/support/api/methods/
"""

import os
import re
import xml.etree.ElementTree as ET
try:
    import requests
except ModuleNotFoundError:
    requests = None
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return False

load_dotenv()

# --- Config ---
API_USER = os.getenv("NAMECHEAP_API_USER", "")
API_KEY = os.getenv("NAMECHEAP_API_KEY", "")
USERNAME = os.getenv("NAMECHEAP_USERNAME", "")
CLIENT_IP = os.getenv("NAMECHEAP_CLIENT_IP", "")

PRODUCTION_URL = "https://api.namecheap.com/xml.response"
SANDBOX_URL = "https://api.sandbox.namecheap.com/xml.response"

# Use production by default; set NAMECHEAP_SANDBOX=1 in .env to use sandbox
USE_SANDBOX = os.getenv("NAMECHEAP_SANDBOX", "0") == "1"
API_URL = SANDBOX_URL if USE_SANDBOX else PRODUCTION_URL

# Cheapest TLDs to check, ordered by actual 1-year price (cheapest first)
# .online/$0.98  .site/$0.98  .xyz/$2.00  .uk/$6.98  .co.uk/~$7  .org.uk/~$7  .com/~$9
CHEAP_TLDS = [".online", ".site", ".xyz", ".uk", ".co.uk", ".org.uk", ".com"]

# XML namespace used in Namecheap responses
NS = {"nc": "http://api.namecheap.com/xml.response"}
NS_HTTPS = {"nc": "https://api.namecheap.com/xml.response"}


def _base_params():
    """Return the global params required for every API call."""
    return {
        "ApiUser": API_USER,
        "ApiKey": API_KEY,
        "UserName": USERNAME,
        "ClientIp": CLIENT_IP,
    }


def _api_call(command, extra_params=None, method="GET"):
    """Make a Namecheap API call and return parsed XML root + status."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required for live Namecheap API calls.")
    params = _base_params()
    params["Command"] = f"namecheap.{command}"
    if extra_params:
        params.update(extra_params)

    if method == "GET":
        resp = requests.get(API_URL, params=params, timeout=30)
    else:
        resp = requests.post(API_URL, data=params, timeout=30)

    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    # Detect namespace (http vs https)
    ns = NS
    if "https://api.namecheap.com" in resp.text:
        ns = NS_HTTPS

    status = root.attrib.get("Status", "ERROR")
    errors: list[str] = []
    error_numbers: list[str] = []
    seen: list = []
    for err_container in [root.find("Errors", ns), root.find("nc:Errors", ns),
                          root.find("{http://api.namecheap.com/xml.response}Errors"),
                          root.find("{https://api.namecheap.com/xml.response}Errors")]:
        # Four guesses at ONE element. For a namespaced response two of them
        # hit the same node, so every error was appended TWICE - and that
        # doubled text went into the money audit log and into what the
        # operator was shown.
        if err_container is None or any(c is err_container for c in seen):
            continue
        seen.append(err_container)
        for err in err_container:
            number = (err.attrib.get("Number") or "").strip()
            text = (err.text or "").strip()
            if number:
                error_numbers.append(number)
            # Keep the NUMBER. It is the only part a person can look up in
            # Namecheap's error list, and discarding it left whoever was
            # called in with a sentence and nothing to search for.
            errors.append(f"{number}: {text}" if number and text
                          else (text or number or "Unknown error"))

    return {"root": root, "status": status, "errors": errors,
            "error_numbers": error_numbers, "raw": resp.text, "ns": ns}


# ============================================================
# DOMAIN AVAILABILITY CHECK
# ============================================================

def check_domains(domain_list):
    """
    Check availability of a list of domains (max 50 per call).
    Returns list of dicts with domain, available, is_premium, price info.
    """
    if not domain_list:
        return []

    # Namecheap allows max 50 per call
    results = []
    for i in range(0, len(domain_list), 50):
        batch = domain_list[i:i + 50]
        domain_str = ",".join(batch)
        resp = _api_call("domains.check", {"DomainList": domain_str})

        if resp["status"] != "OK":
            return {"error": f"API error: {resp['errors']}"}

        # Parse results - try both namespace variants
        for node in resp["root"].iter():
            if "DomainCheckResult" in node.tag:
                results.append({
                    "domain": node.attrib.get("Domain", ""),
                    "available": node.attrib.get("Available", "false").lower() == "true",
                    "is_premium": node.attrib.get("IsPremiumName", "false").lower() == "true",
                    "registration_price": node.attrib.get("PremiumRegistrationPrice", "0"),
                    "icann_fee": node.attrib.get("IcannFee", "0"),
                })

    return results


# A DNS label cannot exceed 63 characters, and namecheap.domains.check takes
# at most 50 domains per call (check_domains batches on exactly that).
MAX_LABEL_LEN = 63
MAX_CANDIDATES = 50


def _domain_bases(company_name):
    """Second-level labels worth trying, best first.

    This used to be one label - the cleaned company name - plus an
    abbreviation, and the abbreviation only when the cleaned name ran past 15
    characters. Seven candidates in all. A company whose name is already taken
    on all seven cheap TLDs stopped the pipeline and waited for a person to
    choose a domain by hand, and that is the commonest reason this stage needs
    one.

    An availability check is not billed, so there is no reason to stop at
    seven. What there IS reason to stop at: 50, which is what a single
    domains.check call carries, and 63 characters, which is what a DNS label
    carries - a longer label can only be refused, and a refusal from the
    registry is the very failure this is trying to avoid.
    """
    clean = _company_name_to_domain(company_name)
    if not clean:
        return []
    words = [w for w in clean.split("-") if w]
    abbrev = "".join(w[0] for w in words)

    # Best first: the name as it reads, then without hyphens, then with a
    # suffix a UK company plausibly uses, then shortened to its first words,
    # and last the initials - least recognisable, so least useful on a Play
    # Console listing, but better than stopping.
    ordered = [
        clean,
        clean.replace("-", ""),
        f"{clean}-ltd",
        f"{clean}-uk",
        "-".join(words[:3]),
        "-".join(words[:2]),
        abbrev,
    ]
    bases, seen = [], set()
    for b in ordered:
        if not b or b in seen or len(b) < 2 or len(b) > MAX_LABEL_LEN:
            continue
        seen.add(b)
        bases.append(b)
    return bases[:max(1, MAX_CANDIDATES // len(CHEAP_TLDS))]


def find_cheapest_domain(company_name):
    """
    Given a company name, generate domain candidates and find the cheapest available one.
    Returns the best option or None.
    """
    bases = _domain_bases(company_name)
    if not bases:
        return {"error": "Could not generate domain name from company name"}

    candidates = [f"{b}{tld}" for b in bases for tld in CHEAP_TLDS]
    base_rank = {b: i for i, b in enumerate(bases)}

    results = check_domains(candidates)
    if isinstance(results, dict) and "error" in results:
        return results

    available = [r for r in results if r["available"] and not r["is_premium"]]
    if not available:
        return {"error": "No available domains found", "checked": candidates}

    # Sort by TLD preference (cheapest first from our list)
    def tld_priority(d):
        for i, tld in enumerate(CHEAP_TLDS):
            if d["domain"].endswith(tld):
                return i
        return 99

    # Price first, then how well the label spells the company: among equally
    # cheap TLDs the name itself beats "name-ltd", which beats the initials.
    available.sort(key=lambda d: (tld_priority(d),
                                  base_rank.get(d["domain"].split(".")[0], 99)))
    return {"best": available[0], "alternatives": available[1:5]}


def _company_name_to_domain(company_name):
    """Convert a company name to a valid domain-safe string."""
    name = company_name.upper()
    # Remove common suffixes
    for suffix in ["LIMITED", "LTD", "PLC", "LLP", "INC", "CORP", "LLC"]:
        name = re.sub(rf"\b{suffix}\b", "", name)
    # Lowercase, strip non-alphanumeric, collapse hyphens
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    # Collapse repeated hyphens
    name = re.sub(r"-+", "-", name)
    return name if len(name) >= 2 else None


# ============================================================
# DOMAIN REGISTRATION
# ============================================================

def register_domain(domain_name, years=1, registrant_info=None):
    """
    Register a domain. registrant_info is a dict with contact fields.
    If not provided, uses minimal defaults from env.

    Required registrant fields:
        FirstName, LastName, Address1, City, StateProvince,
        PostalCode, Country, Phone (+NNN.NNNNNNNNNN), EmailAddress
    """
    if not registrant_info:
        return {"error": "registrant_info dict is required with contact details"}

    sld, tld = _split_domain(domain_name)
    if not sld or not tld:
        return {"error": f"Could not parse domain: {domain_name}"}

    params = {
        "DomainName": domain_name,
        "Years": str(years),
        "AddFreeWhoisguard": "yes",
        "WGEnabled": "yes",
    }

    # Set all 4 contact types to the same info
    for prefix in ["Registrant", "Tech", "Admin", "AuxBilling"]:
        params[f"{prefix}FirstName"] = registrant_info.get("FirstName", "")
        params[f"{prefix}LastName"] = registrant_info.get("LastName", "")
        params[f"{prefix}Address1"] = registrant_info.get("Address1", "")
        params[f"{prefix}City"] = registrant_info.get("City", "")
        params[f"{prefix}StateProvince"] = registrant_info.get("StateProvince", "")
        params[f"{prefix}PostalCode"] = registrant_info.get("PostalCode", "")
        params[f"{prefix}Country"] = registrant_info.get("Country", "GB")
        params[f"{prefix}Phone"] = registrant_info.get("Phone", "")
        params[f"{prefix}EmailAddress"] = registrant_info.get("EmailAddress", "")
        if registrant_info.get("OrganizationName"):
            params[f"{prefix}OrganizationName"] = registrant_info["OrganizationName"]

    resp = _api_call("domains.create", params, method="POST")

    if resp["status"] != "OK":
        return {"error": f"Registration failed: {resp['errors']}",
                "error_numbers": resp.get("error_numbers", []),
                "raw": resp["raw"]}

    # Parse result
    for node in resp["root"].iter():
        if "DomainCreateResult" in node.tag:
            return {
                "registered": node.attrib.get("Registered", "false").lower() == "true",
                "domain": node.attrib.get("Domain", ""),
                "charged": node.attrib.get("ChargedAmount", ""),
                "domain_id": node.attrib.get("DomainID", ""),
                "order_id": node.attrib.get("OrderID", ""),
                "whoisguard": node.attrib.get("WhoisguardEnable", ""),
            }

    return {"error": "Unexpected response format", "raw": resp["raw"]}


def _node_name(node):
    return node.tag.rsplit("}", 1)[-1]


def _child_text(node, child_name, default=""):
    for child in list(node):
        if _node_name(child).lower() == child_name.lower():
            return child.text or default
    return default


def get_address_list():
    """Get saved Namecheap account address profiles."""
    resp = _api_call("users.address.getList")
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    addresses = []
    for node in resp["root"].iter():
        if "List" in _node_name(node) and node.attrib.get("AddressId"):
            addresses.append({
                "address_id": node.attrib.get("AddressId", ""),
                "address_name": node.attrib.get("AddressName", ""),
            })
    return {"addresses": addresses, "count": len(addresses)}


def get_address_info(address_id):
    """Get a saved Namecheap account address profile by AddressId."""
    resp = _api_call("users.address.getInfo", {"AddressId": str(address_id)})
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    for node in resp["root"].iter():
        if _node_name(node) == "GetAddressInfoResult":
            return _contact_from_xml_node(node, source=f"users.address.getInfo:{address_id}")
    return {"error": "Could not parse address info", "raw": resp["raw"]}


def get_domain_contacts(domain_name):
    """Get registrant/admin/tech/billing contacts for a domain in this account."""
    resp = _api_call("domains.getContacts", {"DomainName": domain_name})
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    contacts = {}
    for node in resp["root"].iter():
        name = _node_name(node)
        if name in {"Registrant", "Admin", "Tech", "AuxBilling"}:
            contacts[name] = _contact_from_xml_node(node, source=f"domains.getContacts:{domain_name}:{name}")

    if not contacts:
        return {"error": "Could not parse domain contacts", "raw": resp["raw"]}
    return {"domain": domain_name, "contacts": contacts}


def _contact_from_xml_node(node, source=""):
    """Convert Namecheap contact XML into domains.create contact fields."""
    return {
        "FirstName": _child_text(node, "FirstName"),
        "LastName": _child_text(node, "LastName"),
        "OrganizationName": _child_text(node, "OrganizationName") or _child_text(node, "Organization"),
        "JobTitle": _child_text(node, "JobTitle"),
        "Address1": _child_text(node, "Address1"),
        "Address2": _child_text(node, "Address2"),
        "City": _child_text(node, "City"),
        "StateProvince": _child_text(node, "StateProvince"),
        "PostalCode": _child_text(node, "PostalCode") or _child_text(node, "Zip"),
        "Country": _child_text(node, "Country") or "GB",
        "Phone": _child_text(node, "Phone"),
        "PhoneExt": _child_text(node, "PhoneExt"),
        "EmailAddress": _child_text(node, "EmailAddress"),
        "source": source,
    }


def get_existing_account_contact(reference_domain=None, address_id=None):
    """
    Get a Namecheap contact template from the existing account.

    Priority:
      1. explicit address_id or NAMECHEAP_DEFAULT_ADDRESS_ID
      2. explicit reference_domain or NAMECHEAP_REFERENCE_DOMAIN
      3. first saved address profile
    """
    address_id = address_id or os.getenv("NAMECHEAP_DEFAULT_ADDRESS_ID", "")
    reference_domain = reference_domain or os.getenv("NAMECHEAP_REFERENCE_DOMAIN", "")

    if address_id:
        info = get_address_info(address_id)
        if "error" not in info:
            return info

    if reference_domain:
        contacts = get_domain_contacts(reference_domain)
        if "error" not in contacts:
            registrant = contacts.get("contacts", {}).get("Registrant")
            if registrant:
                return registrant

    addresses = get_address_list()
    if "error" in addresses or not addresses.get("addresses"):
        return {"error": addresses.get("error", "No saved address profiles found")}
    first = addresses["addresses"][0]["address_id"]
    return get_address_info(first)


def _split_domain(domain_name):
    """Split domain into SLD and TLD. Handles multi-part TLDs like .co.uk."""
    multi_tlds = [".co.uk", ".org.uk", ".me.uk", ".com.au", ".net.au",
                  ".org.au", ".co.in", ".com.es", ".org.es", ".nom.es"]
    for tld in multi_tlds:
        if domain_name.endswith(tld):
            sld = domain_name[:-len(tld)]
            return sld, tld.lstrip(".")
    # Simple TLD
    parts = domain_name.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


# ============================================================
# DNS MANAGEMENT - TXT RECORDS
# ============================================================

def get_dns_hosts(domain_name):
    """Get current DNS host records for a domain."""
    sld, tld = _split_domain(domain_name)
    if not sld or not tld:
        return {"error": f"Could not parse domain: {domain_name}"}

    resp = _api_call("domains.dns.getHosts", {"SLD": sld, "TLD": tld})

    if resp["status"] != "OK":
        return {"error": f"Failed to get DNS: {resp['errors']}"}

    hosts = []
    for node in resp["root"].iter():
        if "host" in node.tag.lower() and node.attrib.get("Name"):
            hosts.append({
                "host_id": node.attrib.get("HostId", ""),
                "name": node.attrib.get("Name", ""),
                "type": node.attrib.get("Type", ""),
                "address": node.attrib.get("Address", ""),
                "mx_pref": node.attrib.get("MXPref", ""),
                "ttl": node.attrib.get("TTL", ""),
            })

    return {"domain": domain_name, "hosts": hosts}


def set_dns_hosts(domain_name, hosts):
    """
    Set DNS host records for a domain. REPLACES all existing records.
    
    hosts: list of dicts, each with keys: name, type, address, ttl (optional), mx_pref (optional)
    Example: [
        {"name": "@", "type": "TXT", "address": "google-site-verification=xxx", "ttl": "1800"},
        {"name": "@", "type": "URL", "address": "http://example.com", "ttl": "1800"},
    ]
    """
    sld, tld = _split_domain(domain_name)
    if not sld or not tld:
        return {"error": f"Could not parse domain: {domain_name}"}

    params = {"SLD": sld, "TLD": tld}
    for i, host in enumerate(hosts, 1):
        params[f"HostName{i}"] = host["name"]
        params[f"RecordType{i}"] = host["type"]
        params[f"Address{i}"] = host["address"]
        if host.get("mx_pref"):
            params[f"MXPref{i}"] = host["mx_pref"]
        params[f"TTL{i}"] = host.get("ttl", "1800")

    resp = _api_call("domains.dns.setHosts", params, method="POST")

    if resp["status"] != "OK":
        return {"error": f"Failed to set DNS: {resp['errors']}"}

    for node in resp["root"].iter():
        if "DomainDNSSetHostsResult" in node.tag:
            return {
                "domain": node.attrib.get("Domain", domain_name),
                "success": node.attrib.get("IsSuccess", "false").lower() == "true",
            }

    return {"error": "Unexpected response", "raw": resp["raw"]}


def add_txt_record(domain_name, txt_value, hostname="@"):
    """
    Add a TXT record while preserving existing DNS records.
    This is the key method for Google Search Console verification.
    """
    # Get existing records first
    current = get_dns_hosts(domain_name)
    if "error" in current:
        return current

    # Build new host list: existing + new TXT
    hosts = []
    for h in current.get("hosts", []):
        # Skip parking page records if present
        if h["type"] == "URL" and "parkingpage" in h.get("address", "").lower():
            continue
        hosts.append({
            "name": h["name"],
            "type": h["type"],
            "address": h["address"],
            "ttl": h.get("ttl", "1800"),
            "mx_pref": h.get("mx_pref", ""),
        })

    # Add the new TXT record
    hosts.append({
        "name": hostname,
        "type": "TXT",
        "address": txt_value,
        "ttl": "1800",
    })

    return set_dns_hosts(domain_name, hosts)


def prepare_google_txt_record(domain_name, txt_value="", hostname="@"):
    """
    Apply a Google verification TXT record if a token is available.

    If txt_value is blank, this returns a pending status and makes no DNS
    changes. This keeps the pipeline Google-safe while still recording the
    exact Namecheap action to run after a human obtains the token.
    """
    txt_value = (txt_value or "").strip()
    hostname = (hostname or "@").strip()
    if not txt_value:
        return {
            "status": "token_pending",
            "domain": domain_name,
            "hostname": hostname,
            "value": "",
            "message": "Paste Google's TXT token, then rerun DNS setup.",
        }
    result = add_txt_record(domain_name, txt_value, hostname=hostname)
    if "error" in result:
        result.update({"status": "error", "hostname": hostname, "value": txt_value})
        return result
    result.update({"status": "configured", "hostname": hostname, "value": txt_value})
    return result


def set_default_dns(domain_name):
    """Set domain to use Namecheap's default DNS servers (required for host records)."""
    sld, tld = _split_domain(domain_name)
    if not sld or not tld:
        return {"error": f"Could not parse domain: {domain_name}"}

    resp = _api_call("domains.dns.setDefault", {"SLD": sld, "TLD": tld})
    if resp["status"] != "OK":
        return {"error": f"Failed to set default DNS: {resp['errors']}"}
    return {"success": True, "domain": domain_name}


# ============================================================
# EMAIL FORWARDING
# ============================================================

def set_email_forwarding_rules(domain_name, rules):
    """
    Set one or more email forwarding rules.

    Namecheap treats setEmailForwarding as the complete forwarding set, so call
    get_email_forwarding first and merge rules when preserving existing aliases.
    """
    params = {"DomainName": domain_name}
    for i, rule in enumerate(rules, 1):
        params[f"MailBox{i}"] = rule["mailbox"]
        params[f"ForwardTo{i}"] = rule["forward_to"]
    resp = _api_call("domains.dns.setEmailForwarding", params)

    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    for node in resp["root"].iter():
        if "SetEmailForwardingResult" in node.tag or "DomainDNSSetEmailForwardingResult" in node.tag:
            return {
                "success": node.attrib.get("IsSuccess", "false").lower() == "true",
                "domain": domain_name,
                "forwarding": [
                    f"{r['mailbox']}@{domain_name} -> {r['forward_to']}"
                    for r in rules
                ],
            }

    return {"success": True, "domain": domain_name}


def set_email_forwarding(domain_name, mailbox, forward_to):
    """
    Set email forwarding: mailbox@domain -> forward_to.
    e.g. set_email_forwarding("demanda.co.uk", "info", "your@gmail.com")
    """
    return set_email_forwarding_rules(
        domain_name,
        [{"mailbox": mailbox, "forward_to": forward_to}],
    )


def get_email_forwarding(domain_name):
    """Get current Namecheap email forwarding rules for a domain."""
    resp = _api_call("domains.dns.getEmailForwarding", {"DomainName": domain_name})
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    forwards = []
    for node in resp["root"].iter():
        if _node_name(node) == "Forward":
            mailbox = node.attrib.get("mailbox", "")
            forwards.append({
                "mailbox": mailbox,
                "address": node.text or "",
            })
    return {"domain": domain_name, "forwarding": forwards}


def ensure_dev_email_forwarding(domain_name, forward_to):
    """Create/confirm dev@domain -> forward_to forwarding."""
    if not domain_name:
        return {"status": "missing_domain", "error": "domain is required"}
    if not forward_to:
        return {"status": "missing_forward_target", "error": "forward_to is required"}

    current = get_email_forwarding(domain_name)
    if "error" not in current:
        developer_email = f"dev@{domain_name}".lower()
        target = forward_to.lower()
        for item in current.get("forwarding", []):
            if item.get("mailbox", "").lower() == "dev" and item.get("address", "").lower() == target:
                return {
                    "status": "configured",
                    "success": True,
                    "domain": domain_name,
                    "developer_email": developer_email,
                    "forward_to": forward_to,
                    "message": "dev forwarding already present",
                }

    rules = []
    if "error" not in current:
        for item in current.get("forwarding", []):
            mailbox = item.get("mailbox", "")
            address = item.get("address", "")
            if not mailbox or not address or mailbox.lower() == "dev":
                continue
            rules.append({"mailbox": mailbox, "forward_to": address})
    rules.append({"mailbox": "dev", "forward_to": forward_to})

    result = set_email_forwarding_rules(domain_name, rules)
    if "error" in result:
        return {
            "status": "error",
            "success": False,
            "domain": domain_name,
            "developer_email": f"dev@{domain_name}",
            "forward_to": forward_to,
            "error": result["error"],
        }
    return {
        "status": "configured" if result.get("success") else "error",
        "success": result.get("success", False),
        "domain": domain_name,
        "developer_email": f"dev@{domain_name}",
        "forward_to": forward_to,
        "message": "; ".join(result.get("forwarding", [])) if isinstance(result.get("forwarding"), list) else result.get("forwarding", ""),
    }


# ============================================================
# ACCOUNT INFO
# ============================================================

def get_balance():
    """Get Namecheap account balance."""
    resp = _api_call("users.getBalances")
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    for node in resp["root"].iter():
        if "GetBalancesResult" in node.tag or "UserGetBalancesResult" in node.tag:
            return {
                "available": node.attrib.get("AvailableBalance", ""),
                "account_balance": node.attrib.get("AccountBalance", ""),
                "earned": node.attrib.get("EarnedAmount", ""),
                "currency": node.attrib.get("Currency", "USD"),
            }

    return {"error": "Could not parse balance", "raw": resp["raw"]}


def get_domain_list():
    """Get list of all domains in the account."""
    resp = _api_call("domains.getList", {"PageSize": "100"})
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    domains = []
    for node in resp["root"].iter():
        if "Domain" in node.tag and node.attrib.get("Name"):
            domains.append({
                "name": node.attrib.get("Name", ""),
                "id": node.attrib.get("ID", ""),
                "expires": node.attrib.get("Expires", ""),
                "is_expired": node.attrib.get("IsExpired", ""),
                "auto_renew": node.attrib.get("AutoRenew", ""),
                "whoisguard": node.attrib.get("WhoisGuard", ""),
            })

    return {"domains": domains, "count": len(domains)}


def list_account_domains(max_pages=20):
    """Every domain in THIS account, with an explicit completeness flag.

    Returns {"domains": [{"name": ...}], "complete": bool}, or {"error": ...}.

    `complete` matters more than the list does. "This domain is not in the
    account" is the answer that lets the pipeline go on and buy one, so it may
    only be given when the whole account was actually read. A failed call, a
    listing that hit the page cap, or a page that repeats because the provider
    did not honour our paging all come back complete=False, and the caller
    must then treat a miss as UNKNOWN rather than as "not ours".

    That distinction is the one pipeline.py:180 records for this same call:
    an error response carries no Domain elements, so an unchecked failure
    reads exactly like an account with nothing in it.
    """
    names: list[str] = []
    seen: set[str] = set()
    complete = False
    for page in range(1, max_pages + 1):
        resp = _api_call("domains.getList", {"PageSize": "100", "Page": str(page)})
        if resp["status"] != "OK":
            return {"error": f"Failed: {resp['errors']}",
                    "error_numbers": resp.get("error_numbers", [])}
        page_names = [n.attrib.get("Name", "") for n in resp["root"].iter()
                      if "Domain" in _node_name(n) and n.attrib.get("Name")]
        if not page_names:
            complete = True
            break
        fresh = [n for n in page_names if n.lower() not in seen]
        if not fresh:
            # The same page came back twice, so Page is not being honoured and
            # the rest of the account was never read. Not complete.
            break
        for n in fresh:
            seen.add(n.lower())
            names.append(n)
        if len(page_names) < 100:
            complete = True
            break
    return {"domains": [{"name": n} for n in names], "complete": complete}


def get_pricing(tld="com"):
    """Get registration pricing for a specific TLD."""
    resp = _api_call("users.getPricing", {
        "ProductType": "DOMAIN",
        "ProductCategory": "DOMAINS",
        "ActionName": "REGISTER",
        "ProductName": tld,
    })
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}

    prices = []
    for node in resp["root"].iter():
        if "Price" in node.tag and node.attrib.get("Duration"):
            prices.append({
                "duration": node.attrib.get("Duration", ""),
                "duration_type": node.attrib.get("DurationType", ""),
                "price": node.attrib.get("Price", ""),
                "regular_price": node.attrib.get("RegularPrice", ""),
                "your_price": node.attrib.get("YourPrice", ""),
                "currency": node.attrib.get("Currency", "USD"),
            })

    return {"tld": tld, "prices": prices}


# ============================================================
# WHOIS PRIVACY
# ============================================================

def enable_privacy(domain_name):
    """Enable WHOIS privacy protection for a domain."""
    resp = _api_call("domainprivacy.enable", {"DomainName": domain_name})
    if resp["status"] != "OK":
        return {"error": f"Failed: {resp['errors']}"}
    return {"success": True, "domain": domain_name}
