/**
 * pipeline-privacy - serves /privacy on each company's own domain.
 *
 * WHY THIS EXISTS. The pipeline used to create an account on
 * freeprivacypolicy.com per company and scrape a policy out of it. That site
 * is behind a bot challenge and now answers 403 "Just a moment..." to both
 * login and register, so the step fails every time. It is also the wrong
 * shape: a legal document that every one of these companies depends on
 * should not live behind someone else's signup flow, one account per
 * company, revocable at their discretion.
 *
 * Play Console requires a privacy policy URL and will not accept an app
 * without one. Each company already owns a domain whose DNS we control, so
 * the policy goes there.
 *
 * WHAT IS IN KV AND WHAT IS IN THIS FILE. KV holds FACTS about one company:
 * its registered name and number, its contact address, and what the app
 * actually does with data. This file holds the WORDING. That split is
 * deliberate - correcting a sentence is then one deploy rather than
 * rewriting a record per company, and no company can drift onto an older
 * version of the text.
 *
 * THE FACTS ARE NOT WRITTEN BY HAND. privacy_site.py derives them from
 * play_declarations.scan_app_facts - the same function behind the Play
 * Data safety answers - so the policy and the declarations cannot contradict
 * each other in front of a reviewer. If the app ever gains a network
 * permission or a tracking SDK, that function stops reporting "collects
 * nothing" and the publisher refuses to write a record at all.
 *
 * AN UNKNOWN HOST GETS 404, NEVER A POLICY. Serving a generic "we collect
 * nothing" page for a domain we hold no record of would be publishing a
 * legal claim about software we have not inspected. Missing is recoverable;
 * false is not.
 */

const ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESCAPE[c]);
}

function notFound(reason) {
  // The reason is for whoever is debugging, not for a visitor to act on.
  return new Response(`Not found\n\n${reason}\n`, {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function renderPolicy(host, f) {
  const company = esc(f.company_name);
  const number = esc(f.company_number);
  const contact = esc(f.contact_email);
  const app = esc(f.app_name || f.company_name);
  const updated = esc(f.updated);
  const address = f.registered_address ? esc(f.registered_address) : "";

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy &middot; ${company}</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --muted:#5a5a5a; --rule:#e3e3e3; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e8e8e8; --bg:#161616; --muted:#a0a0a0; --rule:#333; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.65 -apple-system,
         BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 42rem; margin: 0 auto; padding: 3rem 16px 5rem; }
  h1 { font-size: 1.6rem; line-height:1.25; margin: 0 0 .25rem; }
  h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; }
  .sub { color: var(--muted); margin: 0 0 2rem; font-size: .9rem; }
  hr { border:0; border-top:1px solid var(--rule); margin: 2.5rem 0 1.5rem; }
  footer { color: var(--muted); font-size: .85rem; }
  a { color: inherit; }
</style>
</head>
<body>
<main>
  <h1>Privacy Policy</h1>
  <p class="sub">${app} &middot; last updated ${updated}</p>

  <p>This policy explains what the ${app} Android application does with
     information. It is published by ${company}${number ? `, a company registered in England and Wales under number ${number}` : ""}.</p>

  <h2>What the app collects</h2>
  <p><strong>Nothing.</strong> The application collects no personal
     information, and it does not transmit any information anywhere.</p>
  <p>This is not a promise about our intentions. The application requests
     no Android permissions at all, including no internet permission, so
     the operating system does not permit it to send data off the device
     even if it tried to.</p>

  <h2>Information you enter</h2>
  <p>Anything you record in the app is written to the app's own private
     storage on your device. It is not uploaded, synchronised or backed up
     to us or to anyone else. Uninstalling the application removes it.</p>

  <h2>Third parties</h2>
  <p>The application contains no advertising, no analytics, no crash
     reporting and no third-party tracking components of any kind. No data
     is shared with third parties, because no data leaves your device.</p>

  <h2>Accounts and deletion</h2>
  <p>There are no accounts and no sign-in. We hold no record of you, so
     there is nothing for us to delete on request. To remove everything the
     app has stored, uninstall it.</p>

  <h2>Children</h2>
  <p>The application is intended for adults in a workplace setting. It is
     not directed at children, and since it collects nothing it collects
     nothing from children.</p>

  <h2>Changes</h2>
  <p>If the application changes so that any of the above stops being
     accurate, this page is updated before that version is published.</p>

  <h2>Contact</h2>
  <p>Questions about this policy: <a href="mailto:${contact}">${contact}</a>${address ? `<br>${address}` : ""}</p>

  <hr>
  <footer>${company}${number ? ` &middot; registered in England and Wales no. ${number}` : ""} &middot; ${esc(host)}</footer>
</main>
</body>
</html>
`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path !== "/privacy") {
      // Only the policy is published. Everything else, including the apex,
      // is a clean 404 rather than a placeholder page nobody maintains.
      return notFound(`nothing is published at ${path} on ${host}`);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed\n", {
        status: 405, headers: { allow: "GET, HEAD" },
      });
    }
    if (!env || !env.POLICIES) {
      // A missing binding must not fall through to a default policy.
      return new Response("Misconfigured: no POLICIES binding\n", {
        status: 500, headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    const raw = await env.POLICIES.get(host);
    if (!raw) {
      return notFound(
        `no policy record for ${host}; nothing is published for a domain ` +
        `whose application has not been inspected`);
    }

    let facts;
    try {
      facts = JSON.parse(raw);
    } catch (e) {
      return new Response("Misconfigured: policy record is not JSON\n", {
        status: 500, headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    // The publisher refuses to write a record it could not derive, but the
    // check is repeated here: a record hand-edited into KV must not be able
    // to publish a claim nobody verified.
    if (facts.collects_data !== false) {
      return notFound(
        `the record for ${host} does not assert that the app collects ` +
        `nothing, so the policy below it would not be accurate`);
    }

    return new Response(renderPolicy(host, facts), {
      status: 200,
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
      },
    });
  },
};
