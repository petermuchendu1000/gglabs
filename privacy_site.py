"""Publish each company's privacy policy on its own domain.

REPLACES freeprivacypolicy.com. That route created an account per company
through someone else's signup flow and scraped a URL out of it; the site now
sits behind a bot challenge and answers 403 "Just a moment..." to both login
and register, so it fails every time. Defeating that challenge is not an
option, and it was the wrong dependency anyway: Play Console will not accept
an app without a policy URL, which makes it load-bearing, and a load-bearing
legal document should not be revocable by a third party none of these
companies has a relationship with.

HOW IT WORKS. One Worker (workers/pipeline_privacy.js) and one KV namespace
serve every company. Per company this module:

  1. derives the facts,
  2. writes them to KV under the domain name,
  3. attaches the domain to the Worker as a Workers custom domain, which
     makes Cloudflare create the DNS record and issue the certificate.

The wording lives in the Worker, the facts live in KV. Correcting a sentence
is then one deploy rather than a rewrite per company, and no company can be
left on an older version of the text.

THE ONE RULE THAT MATTERS. The facts come from
play_declarations.scan_app_facts - the same function behind the Play Data
safety answers. A privacy policy that contradicts the Data safety
declaration, in front of the reviewer reading both, is worse than no policy
at all, so they are not allowed to have separate sources. If the app gains a
network permission or a tracking dependency, scan_app_facts stops supporting
"collects nothing" and this module REFUSES TO PUBLISH rather than leaving a
page up that has quietly become false.

WHAT THIS MODULE DOES NOT CLAIM. It produces an accurate description of what
the application does. It does not certify that the description satisfies any
particular jurisdiction's law, and nobody should treat it as legal advice.
Its accuracy is checkable - that is the property worth having here, and the
reason every sentence in the Worker maps to something scan_app_facts
observed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

API_BASE = "https://api.cloudflare.com/client/v4"

WORKER_SCRIPT = "pipeline-privacy"
KV_NAMESPACE_TITLE = "pipeline-privacy-policies"
POLICY_PATH = "/privacy"


def policy_action(existing_url: str, domain: str) -> tuple[str, str]:
    """Should the build publish again, or keep what is recorded?

    Returns ("publish"|"keep", reason).

    The build used to skip the publisher whenever manifest.json already held
    a URL - correct for freeprivacypolicy.com, where every run created
    another account and another agreement, and wrong for a page we serve
    ourselves. Skipping it disables the safeguard this module exists for:
    if the app gains a network permission, publish_policy is meant to
    REFUSE, but it is never called, so the page goes on telling the world
    the operating system will not let the app send data anywhere.

    A URL we do not serve is left alone. Re-publishing cannot fix someone
    else's page, and overwriting a hand-entered URL would lose it.
    """
    existing = (existing_url or "").strip()
    ours = policy_url_for(domain)
    if not existing:
        return "publish", "no policy URL has been recorded for this company"
    if ours and existing == ours:
        return "publish", ("this policy is served from the company's own "
                           "domain and publishing is idempotent, so the "
                           "app's facts are re-checked on every build")
    return "keep", ("%s is not a URL this pipeline serves, so it is left "
                    "exactly as recorded" % existing)


def policy_url_for(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    return "https://%s%s" % (domain, POLICY_PATH) if domain else ""


# ── The facts ──────────────────────────────────────────────────────────────

def build_record(company: dict, app_facts: dict | None = None,
                 today: str | None = None) -> dict:
    """The KV record for one company, or {"error": ...} if it cannot be made.

    Returning an error rather than a partial record is the point. Every
    sentence the Worker renders is a claim about software; if the software
    could not be inspected, or was inspected and no longer supports the
    claim, there is nothing honest to publish.
    """
    import play_declarations

    domain = str(company.get("domain") or "").strip().lower().rstrip(".")
    if not domain:
        return {"error": "the company has no domain, so there is nowhere to "
                         "publish a policy"}
    contact = str(company.get("support_email") or "").strip()
    if not contact:
        return {"error": "the company has no contact address, and a policy "
                         "with no way to reach anyone is not one"}
    name = str(company.get("company_name") or "").strip()
    if not name:
        return {"error": "the company has no registered name"}

    facts = app_facts if app_facts is not None else play_declarations.scan_app_facts()
    if not facts.get("readable"):
        return {"error": "the app template could not be inspected (%s), so "
                         "no claim about what it collects can be published"
                         % "; ".join(facts.get("errors") or ["unknown"])}
    if facts.get("has_network_permission"):
        return {"error": "the app now has a network permission (%s); the "
                         "published policy says no data can leave the device "
                         "and that is no longer true. Update the policy text "
                         "and this check together."
                         % ", ".join(facts.get("permissions") or [])}
    if facts.get("tracking_dependencies"):
        return {"error": "the app now depends on %s, which may collect data; "
                         "'no analytics and no tracking' can no longer be "
                         "published as fact"
                         % ", ".join(facts["tracking_dependencies"])}
    if facts.get("unknown_dependency_groups"):
        return {"error": "the app depends on %s, which has not been assessed; "
                         "an unrecognised library is exactly where a tracker "
                         "arrives, so nothing is published until it is known"
                         % ", ".join(facts["unknown_dependency_groups"])}

    return {
        "domain": domain,
        "company_name": name,
        "company_number": str(company.get("company_number") or "").strip(),
        "app_name": str(company.get("display_name") or name).strip(),
        "contact_email": contact,
        "registered_address": str(company.get("address") or "").strip(),
        # Read by the Worker, which refuses to render unless it is exactly
        # False - so a record edited by hand cannot publish a claim either.
        "collects_data": False,
        "permissions": facts.get("permissions") or [],
        "dependencies": facts.get("dependency_groups") or [],
        "updated": today or date.today().isoformat(),
        "generated_by": "privacy_site.build_record",
    }


# ── Cloudflare ─────────────────────────────────────────────────────────────

def _creds() -> tuple[str, str, str | None]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not token:
        return "", "", "CLOUDFLARE_API_TOKEN is not set"
    if not account:
        return "", "", "CLOUDFLARE_ACCOUNT_ID is not set"
    return token, account, None


def _request(method: str, path: str, token: str, body=None,
             content_type: str = "application/json"):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer %s" % token,
                 "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=60) as f:
            raw = f.read().decode("utf-8", "replace")
            return (json.loads(raw) if raw.strip().startswith(("{", "[")) else
                    {"success": True, "result": raw}), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        return None, "Cloudflare %s %s -> %s: %s" % (method, path, e.code, detail)
    except Exception as e:                      # pragma: no cover - network
        return None, "Cloudflare %s %s failed: %s" % (method, path, e)


def ensure_kv_namespace(title: str = KV_NAMESPACE_TITLE) -> dict:
    """Find the namespace by TITLE, or create it. Never by a remembered id."""
    token, account, err = _creds()
    if err:
        return {"error": err}
    page, err = _request("GET", "/accounts/%s/storage/kv/namespaces?per_page=100"
                         % account, token)
    if err:
        return {"error": err}
    for ns in page.get("result") or []:
        if ns.get("title") == title:
            return {"namespace_id": ns["id"], "created": False}
    made, err = _request("POST", "/accounts/%s/storage/kv/namespaces" % account,
                         token, {"title": title})
    if err:
        return {"error": err}
    return {"namespace_id": (made.get("result") or {}).get("id"), "created": True}


def put_record(namespace_id: str, record: dict) -> dict:
    """Write one company's facts under its domain name."""
    token, account, err = _creds()
    if err:
        return {"error": err}
    domain = record["domain"]
    body = json.dumps(record).encode()
    _resp, err = _request(
        "PUT",
        "/accounts/%s/storage/kv/namespaces/%s/values/%s"
        % (account, namespace_id, urllib.parse.quote(domain, safe="")),
        token, body, content_type="text/plain")
    if err:
        return {"error": err}
    return {"written": True, "key": domain}


def attach_custom_domain(zone_id: str, hostname: str,
                         script: str = WORKER_SCRIPT) -> dict:
    """Route the hostname to the Worker; Cloudflare makes DNS and the cert.

    Idempotent: attaching a hostname already attached to the same script
    succeeds and reports created=False rather than erroring.
    """
    token, account, err = _creds()
    if err:
        return {"error": err}
    existing, err = _request("GET", "/accounts/%s/workers/domains?hostname=%s"
                             % (account, urllib.parse.quote(hostname)), token)
    if err:
        return {"error": err}
    for d in existing.get("result") or []:
        if d.get("hostname") == hostname and d.get("service") == script:
            return {"attached": True, "created": False, "id": d.get("id")}
    made, err = _request("PUT", "/accounts/%s/workers/domains" % account, token,
                         {"zone_id": zone_id, "hostname": hostname,
                          "service": script, "environment": "production"})
    if err:
        return {"error": err}
    return {"attached": True, "created": True,
            "id": (made.get("result") or {}).get("id")}


# ── The one call the pipeline makes ────────────────────────────────────────

def publish_policy(company: dict, app_facts: dict | None = None,
                   dry_run: bool = False) -> dict:
    """Publish the policy for one company. Idempotent; safe to re-run.

    status is exactly one of:
      published        the record is in KV and the domain routes to the Worker
      not_delegated    the domain has no Cloudflare zone yet
      refused          the facts do not support the published text
      error            Cloudflare could not be reached or refused
    """
    import cloudflare_dns

    record = build_record(company, app_facts)
    if "error" in record:
        # Not an outage. The text would be false, so nothing is published.
        return {"status": "refused", "reason": record["error"],
                "domain": str(company.get("domain") or "")}
    domain = record["domain"]

    zone = cloudflare_dns.find_zone(domain)
    if "error" in zone:
        return {"status": "error", "domain": domain, "error": zone["error"]}
    if not zone.get("found"):
        return {"status": "not_delegated", "domain": domain,
                "message": "%s has no zone in this Cloudflare account, so the "
                           "policy cannot be served from it until the "
                           "delegate stage has run" % domain}

    if dry_run:
        return {"status": "published", "domain": domain, "dry_run": True,
                "url": policy_url_for(domain), "record": record}

    ns = ensure_kv_namespace()
    if "error" in ns:
        return {"status": "error", "domain": domain, "error": ns["error"]}
    wrote = put_record(ns["namespace_id"], record)
    if "error" in wrote:
        return {"status": "error", "domain": domain, "error": wrote["error"]}

    attached = attach_custom_domain(zone["zone_id"], domain)
    if "error" in attached:
        return {"status": "error", "domain": domain, "error": attached["error"],
                "record_written": True}

    return {"status": "published", "domain": domain,
            "url": policy_url_for(domain),
            "namespace_id": ns["namespace_id"],
            "domain_attached": attached.get("created"),
            "record": record}
