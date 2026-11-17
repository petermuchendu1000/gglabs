"""Move a domain's DNS from the registrar to Cloudflare, unattended.

This is the step between buying a domain and configuring mail on it. It is
the only place in the pipeline that spans BOTH providers, which is why it
lives in its own module: namecheap_automation knows nothing about Cloudflare
and cloudflare_dns knows nothing about the registrar, and neither should.

The division of labour, once more, because it is the thing most likely to be
undone by accident: NAMECHEAP IS THE REGISTRAR. It buys the domain and sets
its nameservers - the one DNS-adjacent operation only a registrar can do.
CLOUDFLARE owns everything inside the zone.

Order matters and is not arbitrary:

  1. Read the registrar's nameservers FIRST. A domain already delegated has
     no host records at the registrar, so asking for them would fail and the
     failure would look like a problem rather than the expected state.
  2. Create (or adopt) the Cloudflare zone and read back the nameservers
     Cloudflare assigned to THAT zone. Never a constant: Cloudflare rotates
     which pair it hands out, and a stale pair delegates a domain to servers
     that do not serve it - a zone stuck pending for ever, with no error.
  3. Only then write to the registrar, and verify by reading back.
  4. Poll for activation. Registry propagation is minutes to hours, so
     "not active yet" is a normal state, not a failure.

Every outcome is a distinct status. "Not delegated yet", "delegated but not
yet active", "refused because the domain is serving something" and "the
provider could not be reached" are four different operator actions, and
collapsing any of them into the others is the defect family this codebase
has spent its life removing.
"""

import os

import cloudflare_dns
import namecheap_automation


# A record whose address carries one of these is the registrar's own parking
# page, not content anybody would miss.
_PARKING_MARKERS = ("parkingpage", "registrar-servers.com")

# Types that only ever appear on a site's apex or www, as opposed to mail
# (MX), verification (TXT) or a real subdomain.
_WEB_TYPES = ("A", "AAAA", "CNAME", "URL", "URL301", "FRAME")


def _looks_parked(hosts):
    """Is this record set safe to discard by delegating away?

    Returns (parked: bool, serving: list) - the records that argue against.

    DELIBERATELY NOT A PERFECT TEST, and the limit is worth stating: an A
    record on the apex pointing at a real website is indistinguishable here
    from one pointing at a parking page, so this will NOT catch "the domain
    serves a site". What it does catch is the set of things that are
    unambiguously not parking and are expensive to lose: MX records (mail
    routing), TXT records (domain verification somebody did by hand) and
    records on any name other than @ or www (real subdomains).

    The caller always logs the full record set regardless, so an operator
    can see exactly what delegation would discard.
    """
    serving = []
    for h in hosts or []:
        name = str(h.get("name") or "").strip().lower()
        rtype = str(h.get("type") or "").strip().upper()
        address = str(h.get("address") or "").strip().lower()
        if any(marker in address for marker in _PARKING_MARKERS):
            continue
        if name in ("@", "www", "") and rtype in _WEB_TYPES:
            continue
        serving.append({"name": name or "@", "type": rtype, "address": address})
    return (not serving), serving


def delegation_state(domain):
    """Where is this domain right now? READ-ONLY, writes nothing.

    {"registrar_nameservers": [...], "on_cloudflare": bool,
     "zone_id": str|None, "zone_status": str|None, "active": bool,
     "zone_known": bool}
    or {"error": ...}.

    zone_known is not decoration. When Cloudflare cannot be reached - no
    credential, an outage - the registrar half still answers, so this returns
    a useful partial result rather than throwing it away. But "active": False
    would then mean "we could not ask", which reads identically to "the zone
    is not active". zone_known=False marks that difference, and a caller
    must check it before trusting `active`.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return {"error": "domain is required"}

    ns = namecheap_automation.get_nameservers(domain)
    if "error" in ns:
        return {"error": "could not read the registrar's nameservers for %s: %s"
                         % (domain, ns["error"])}
    registrar_ns = ns["nameservers"]
    on_cf = bool(registrar_ns) and all(
        h.endswith(cloudflare_dns.CF_NS_SUFFIX) for h in registrar_ns)

    out = {"domain": domain, "registrar_nameservers": registrar_ns,
           "on_cloudflare": on_cf, "zone_id": None, "zone_status": None,
           "active": False, "zone_known": False}

    zone = cloudflare_dns.find_zone(domain)
    if "error" in zone:
        # Could not ASK. `active` stays False but zone_known stays False too,
        # so nobody reads this as "the zone is not active".
        out["zone_lookup_error"] = zone["error"]
        return out
    out["zone_known"] = True
    if zone.get("found"):
        out["zone_id"] = zone.get("zone_id")
        out["zone_status"] = zone.get("status")
        out["active"] = bool(zone.get("active"))
        out["assigned_nameservers"] = zone.get("name_servers")
    return out


def delegate_domain(domain, force=False, wait=True,
                    timeout=cloudflare_dns.DEFAULT_ACTIVATION_TIMEOUT,
                    poll_interval=cloudflare_dns.DEFAULT_POLL_INTERVAL,
                    _sleep=None, _now=None):
    """Delegate `domain` to Cloudflare. Idempotent; safe to re-run.

    status is exactly one of:
      active               delegated AND Cloudflare reports the zone active
      pending_activation   nameservers are set; Cloudflare has not seen them
                           yet. Normal for minutes to hours. Re-run later.
      refused_serving      the domain still has records that delegating would
                           discard (mail, verification or real subdomains).
                           Pass force=True to go ahead anyway.
      error                a provider could not be reached or refused.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return {"status": "error", "error": "domain is required"}

    # 1. The registrar's own view, first. A delegated domain has no host
    #    records here, so this ordering avoids a misleading failure.
    ns = namecheap_automation.get_nameservers(domain)
    if "error" in ns:
        return {"status": "error", "domain": domain,
                "error": "could not read the registrar's nameservers: %s"
                         % ns["error"]}
    registrar_ns = ns["nameservers"]
    already_on_cf = bool(registrar_ns) and all(
        h.endswith(cloudflare_dns.CF_NS_SUFFIX) for h in registrar_ns)

    # 2. The guard, but only while the registrar still serves the zone.
    #    Once delegated there is nothing left at the registrar to lose.
    records = []
    if not already_on_cf:
        hosts = namecheap_automation.get_dns_hosts(domain)
        if "error" in hosts:
            return {"status": "error", "domain": domain,
                    "error": "could not read the registrar's DNS records, so "
                             "cannot tell what delegation would discard: %s"
                             % hosts["error"]}
        records = hosts.get("hosts") or []
        parked, serving = _looks_parked(records)
        if not parked and not force:
            return {"status": "refused_serving", "domain": domain,
                    "records": records, "serving": serving,
                    "registrar_nameservers": registrar_ns,
                    "message": ("%s still has %d record(s) that delegating "
                                "would discard: %s. Re-run with force=True to "
                                "proceed anyway."
                                % (domain, len(serving),
                                   ", ".join("%s %s" % (r["type"], r["name"])
                                             for r in serving)))}

    # 3. The zone, and the nameservers Cloudflare assigned to IT.
    zone = cloudflare_dns.create_zone(domain)
    if "error" in zone:
        return {"status": "error", "domain": domain, "error": zone["error"],
                "state": zone.get("state"), "records": records}
    zone_id = zone["zone_id"]
    assigned = zone["name_servers"]

    # 4. Point the registrar at them, verified by read-back inside
    #    set_custom_dns. Skipped when they already match.
    if sorted(registrar_ns) == sorted(assigned):
        delegation = {"updated": False, "already": True,
                      "nameservers": registrar_ns, "encoding": "unchanged"}
    else:
        delegation = namecheap_automation.set_custom_dns(domain, assigned)
        if "error" in delegation:
            return {"status": "error", "domain": domain, "zone_id": zone_id,
                    "assigned_nameservers": assigned,
                    "error": delegation["error"],
                    "state": delegation.get("state"), "records": records}

    base = {"domain": domain, "zone_id": zone_id,
            "assigned_nameservers": assigned,
            "previous_nameservers": registrar_ns,
            "zone_created": zone.get("created"),
            "registrar_updated": bool(delegation.get("updated")),
            "encoding": delegation.get("encoding"),
            "records_discarded": records}

    # 5. Activation. Not waiting is a legitimate choice for a batch run.
    if not wait:
        base["status"] = "active" if zone.get("active") else "pending_activation"
        return base

    waited = cloudflare_dns.wait_until_active(
        zone_id, domain, timeout=timeout, poll_interval=poll_interval,
        _sleep=_sleep, _now=_now)
    if "error" in waited:
        base["status"] = "error"
        base["error"] = waited["error"]
        return base
    if waited.get("active"):
        base["status"] = "active"
        base["polls"] = waited.get("polls")
        return base
    base["status"] = "pending_activation"
    base["message"] = waited.get("message") or (
        "%s is delegated but Cloudflare has not seen the nameservers yet; "
        "registry propagation takes minutes to hours. Re-run later." % domain)
    return base
