"""The admin dashboard: who has access, and what they may do.

Access control is NOT repeated here. auth.py's gate refuses anything under
/admin or /api/admin to a caller below admin rank, so every route in this
module is already protected by the time it runs. Re-checking per route
would invite the pattern where one route is added without the check and
nobody notices - the whole point of gating by prefix is that forgetting is
not possible.

What this module does enforce is the difference between admin and
superadmin, because that distinction is per-action rather than per-path:
an admin may approve, suspend and reject people, but only a superadmin may
change roles or delete accounts. Handing out the ability to spend money is
a different act from deciding who may spend it.

The hard invariants - never stranding the system without a superadmin -
live in users.py, not here, because a check in a screen is a suggestion.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template_string, request

import auth
import users

log = logging.getLogger(__name__)
admin = Blueprint("admin", __name__)


def _actor() -> str:
    return auth.actor_name()


def _require_superadmin():
    """None when allowed, else a ready-made error response.

    Asks auth.is_service() explicitly rather than treating "no user object"
    as "must be the service token". Those are different states, and
    conflating them means a request carrying NO identity gets full control.
    Mutation testing produced that exact shape: a session pointing at a
    deleted account fell through the gate with g.user unset, and every
    check here waved it past.
    """
    if auth.is_service():
        return None                      # shared service credential
    u = auth.current_user()
    if u is None:
        return jsonify({"error": "unauthorized"}), 401
    if users.at_least(u["role"], users.ROLE_SUPERADMIN):
        return None
    return jsonify({"error": "Only a superadmin can do that."}), 403


def _self_id() -> int | None:
    u = auth.current_user()
    return u["id"] if u else None


# ── API ─────────────────────────────────────────────────────────────────────
@admin.route("/api/admin/users")
def api_users():
    u = auth.current_user()
    if u is None and not auth.is_service():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "users": users.list_users(),
        "me": {"id": u["id"], "email": u["email"], "role": u["role"]} if u
              else {"id": None, "email": "service-token",
                    "role": users.ROLE_SUPERADMIN},
        "roles": list(users.ROLES),
        "statuses": list(users.STATUSES),
    })


@admin.route("/api/admin/users/<int:user_id>/status", methods=["POST"])
def api_set_status(user_id):
    status = (request.get_json(silent=True) or {}).get("status", "")
    # Suspending yourself is not forbidden by the data layer - it is a
    # legitimate thing for a user to do - but doing it by accident from the
    # admin table is almost never what was meant.
    if user_id == _self_id() and status != users.STATUS_APPROVED:
        return jsonify({"error": "You cannot change your own account's "
                                 "status from here."}), 400
    try:
        return jsonify({"user": users.set_status(user_id, status, _actor())})
    except users.UserError as e:
        return jsonify({"error": str(e)}), 400


@admin.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
def api_set_role(user_id):
    denied = _require_superadmin()
    if denied:
        return denied
    role = (request.get_json(silent=True) or {}).get("role", "")
    if user_id == _self_id():
        return jsonify({"error": "You cannot change your own role."}), 400
    try:
        return jsonify({"user": users.set_role(user_id, role, _actor())})
    except users.UserError as e:
        return jsonify({"error": str(e)}), 400


@admin.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
def api_delete(user_id):
    denied = _require_superadmin()
    if denied:
        return denied
    if user_id == _self_id():
        return jsonify({"error": "You cannot delete your own account."}), 400
    try:
        users.delete_user(user_id, _actor())
        return jsonify({"deleted": True})
    except users.UserError as e:
        return jsonify({"error": str(e)}), 400


@admin.route("/api/admin/users/<int:user_id>/password", methods=["POST"])
def api_reset_password(user_id):
    """Set someone's password for them, flagged so they must change it."""
    denied = _require_superadmin()
    if denied:
        return denied
    pw = (request.get_json(silent=True) or {}).get("password", "")
    try:
        # must_change=True: a password an administrator typed is a password
        # two people know. The epoch bump inside set_password also ends any
        # session that account currently has.
        users.set_password(user_id, pw, actor=_actor(), must_change=True)
        return jsonify({"reset": True})
    except users.UserError as e:
        return jsonify({"error": str(e)}), 400


@admin.route("/api/admin/users", methods=["POST"])
def api_create():
    denied = _require_superadmin()
    if denied:
        return denied
    b = request.get_json(silent=True) or {}
    try:
        user = users.create_user(
            b.get("email", ""), b.get("password", ""),
            full_name=b.get("full_name", ""),
            role=b.get("role", users.ROLE_USER),
            status=users.STATUS_APPROVED,
            must_change_password=True,
            actor=_actor())
        return jsonify({"user": user}), 201
    except users.UserError as e:
        return jsonify({"error": str(e)}), 400


@admin.route("/api/admin/audit")
def api_audit():
    return jsonify({"events": users.audit_tail(200)})


# ── Page ────────────────────────────────────────────────────────────────────
_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Users — Company Pipeline</title>
<style>
  :root{--bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;
        --text:#e6edf3;--muted:#8b949e;--blue:#2f81f7;--green:#3fb950;
        --red:#f85149;--yellow:#d29922}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:var(--bg);
       color:var(--text);line-height:1.5}
  .topbar{background:var(--surface);border-bottom:1px solid var(--border);
          padding:0 20px;height:48px;display:flex;align-items:center;gap:14px}
  .topbar-logo{font-weight:700;font-size:15px}
  .topbar-logo span{color:var(--blue)}
  .spacer{flex:1}
  .tlink{color:var(--muted);text-decoration:none;font-size:12px;font-weight:600;
         padding:5px 10px;border:1px solid var(--border);border-radius:5px}
  .tlink:hover{color:var(--text);border-color:var(--blue)}
  .wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
  h2{font-size:16px;margin:26px 0 10px;font-weight:600}
  .hint{color:var(--muted);font-size:12.5px;margin-bottom:12px}
  table{width:100%;border-collapse:collapse;font-size:13px;
        background:var(--surface);border:1px solid var(--border);border-radius:8px;
        overflow:hidden}
  th,td{text-align:left;padding:9px 11px;border-bottom:1px solid var(--border);
        vertical-align:middle}
  th{color:var(--muted);font-size:11px;text-transform:uppercase;
     letter-spacing:.4px;font-weight:600;background:var(--surface2)}
  tr:last-child td{border-bottom:none}
  tr.pending{background:rgba(210,153,34,.07)}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
        font-weight:600;border:1px solid var(--border)}
  .s-approved{color:var(--green);border-color:var(--green)}
  .s-pending{color:var(--yellow);border-color:var(--yellow)}
  .s-suspended,.s-rejected{color:var(--red);border-color:var(--red)}
  .r-superadmin{color:var(--blue);border-color:var(--blue)}
  button{font-family:inherit;font-size:12px;font-weight:600;padding:4px 9px;
         border-radius:5px;border:1px solid var(--border);background:var(--surface2);
         color:var(--text);cursor:pointer;margin-right:4px}
  button:hover{border-color:var(--blue)}
  button.go{background:var(--green);border-color:var(--green);color:#06230f}
  button.no{background:transparent;border-color:var(--red);color:#ff8b82}
  button:disabled{opacity:.35;cursor:not-allowed}
  select{font-family:inherit;font-size:12px;background:var(--surface2);
         color:var(--text);border:1px solid var(--border);border-radius:5px;
         padding:3px 6px}
  .muted{color:var(--muted);font-size:12px}
  #toast{position:fixed;right:18px;bottom:18px;max-width:420px}
  .t{background:var(--surface);border:1px solid var(--border);border-left-width:3px;
     border-radius:6px;padding:10px 13px;font-size:13px;margin-top:8px}
  .t.err{border-left-color:var(--red)} .t.ok{border-left-color:var(--green)}
</style></head>
<body>
<div class="topbar">
  <div class="topbar-logo">Company <span>Pipeline</span></div>
  <div class="spacer"></div>
  <span class="muted" id="whoami"></span>
  <a class="tlink" href="/">Dashboard</a>
  <a class="tlink" href="/change-password">Change password</a>
  <a class="tlink" href="/logout">Sign out</a>
</div>
<div class="wrap">
  <h2>Waiting for approval <span class="muted" id="pcount"></span></h2>
  <div class="hint">Nobody can sign in until you approve them here.</div>
  <table><thead><tr><th>Name</th><th>Email</th><th>Requested</th><th>Decision</th></tr></thead>
    <tbody id="pending"></tbody></table>

  <h2>Accounts</h2>
  <div class="hint" id="roles-hint"></div>
  <table><thead><tr>
    <th>Name</th><th>Email</th><th>Role</th><th>Status</th>
    <th>Last signed in</th><th>Actions</th></tr></thead>
    <tbody id="users"></tbody></table>

  <h2>Kenyan PSC index</h2>
  <div class="hint">
    Companies House cannot be searched by nationality &mdash; its advanced
    search filters on name, status, type, dates, location and SIC code, and
    nothing else. This index is built from the free daily snapshot of
    People with Significant Control, which does carry nationality, so the
    search runs here instead of against the API.
    <br><br>
    <b>A PSC is not a director.</b> A PSC holds over 25% of shares or votes;
    in a small company that is usually a director too, but it is a different
    fact. These rows are candidates. Open one to ask Companies House whether
    the <i>director</i> rule is actually met.
  </div>
  <div id="psc-status" class="hint">&hellip;</div>
  <div style="margin:8px 0">
    <button id="psc-refresh" onclick="pscRefresh()">Fetch latest snapshot</button>
    <input id="psc-q" placeholder="company number or PSC name"
           oninput="pscSearch()" style="margin-left:8px">
    <label style="margin-left:8px;font-size:12px">
      <input type="checkbox" id="psc-ceased" onchange="pscSearch()">
      include ceased PSCs
    </label>
  </div>
  <pre id="psc-log" style="display:none;max-height:160px;overflow:auto;
       font-size:11px;background:#111;padding:8px;border-radius:4px"></pre>
  <table><thead><tr><th>Company</th><th>PSC</th><th>Born</th>
    <th>Nationality</th><th>Residence</th><th>Control</th>
    <th>Director rule</th></tr></thead>
    <tbody id="psc-rows"></tbody></table>
  <div id="psc-detail"></div>

  <h2>Access log</h2>
  <div class="hint">Every approval, suspension, role change and password reset.</div>
  <table><thead><tr><th>When</th><th>Who</th><th>Did</th><th>To</th><th>Detail</th></tr></thead>
    <tbody id="audit"></tbody></table>
</div>
<div id="toast"></div>
<script>
const esc = s => String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const when = s => s ? new Date(s).toLocaleString() : "\\u2014";
let ME = {role:"user", id:null};

function toast(msg, kind){
  const d=document.createElement("div"); d.className="t "+(kind||"ok"); d.textContent=msg;
  document.getElementById("toast").appendChild(d); setTimeout(()=>d.remove(),5000);
}
async function api(method, path, body){
  const o={method, headers:{}};
  if(body!==undefined){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body);}
  const r=await fetch(path,o); let d={}; try{d=await r.json();}catch(e){}
  if(!r.ok) throw new Error(d.error || ("HTTP "+r.status));
  return d;
}
async function load(){
  let d;
  try { d = await api("GET","/api/admin/users"); }
  catch(e){ toast("Could not load accounts: "+e.message,"err"); return; }
  ME = d.me;
  document.getElementById("whoami").textContent = ME.email+" \\u00b7 "+ME.role;
  const sa = ME.role === "superadmin";
  document.getElementById("roles-hint").textContent = sa
    ? "You are a superadmin: you can change roles, reset passwords and delete accounts."
    : "You can approve and suspend people. Only a superadmin can change roles or delete accounts.";

  const pend = d.users.filter(u=>u.status==="pending");
  document.getElementById("pcount").textContent = pend.length ? "("+pend.length+")" : "(none)";
  document.getElementById("pending").innerHTML = pend.length ? pend.map(u=>`
    <tr class="pending"><td>${esc(u.full_name)||"\\u2014"}</td><td>${esc(u.email)}</td>
    <td>${when(u.created_at)}</td><td>
      <button class="go" onclick="setStatus(${u.id},'approved')">Approve</button>
      <button class="no" onclick="setStatus(${u.id},'rejected')">Reject</button>
    </td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">Nobody is waiting.</td></tr>`;

  document.getElementById("users").innerHTML = d.users.map(u=>{
    const self = u.id === ME.id;
    const roleSel = sa && !self
      ? `<select onchange="setRole(${u.id},this.value)">`+
        d.roles.map(r=>`<option value="${r}" ${r===u.role?"selected":""}>${r}</option>`).join("")+
        `</select>`
      : `<span class="pill r-${esc(u.role)}">${esc(u.role)}</span>`;
    const acts = [];
    if(!self){
      if(u.status==="approved") acts.push(`<button class="no" onclick="setStatus(${u.id},'suspended')">Suspend</button>`);
      else acts.push(`<button class="go" onclick="setStatus(${u.id},'approved')">Approve</button>`);
      if(sa){
        acts.push(`<button onclick="resetPw(${u.id},'${esc(u.email)}')">Reset password</button>`);
        acts.push(`<button class="no" onclick="del(${u.id},'${esc(u.email)}')">Delete</button>`);
      }
    } else acts.push(`<span class="muted">that's you</span>`);
    return `<tr><td>${esc(u.full_name)||"\\u2014"}</td><td>${esc(u.email)}
      ${u.must_change_password?' <span class="pill s-pending">must change password</span>':""}</td>
      <td>${roleSel}</td>
      <td><span class="pill s-${esc(u.status)}">${esc(u.status)}</span></td>
      <td class="muted">${when(u.last_login_at)}</td><td>${acts.join("")}</td></tr>`;
  }).join("");

  try{
    const a = await api("GET","/api/admin/audit");
    document.getElementById("audit").innerHTML = a.events.length ? a.events.map(e=>`
      <tr><td class="muted">${when(e.at)}</td><td>${esc(e.actor)}</td>
      <td>${esc(e.action)}</td><td>${esc(e.subject)}</td>
      <td class="muted">${esc(e.detail)}</td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">Nothing yet.</td></tr>`;
  }catch(e){ /* the table is informational; its failure must not hide the page */ }
}
async function setStatus(id,status){
  try{ await api("POST",`/api/admin/users/${id}/status`,{status});
       toast("Account "+status+".","ok"); load(); }
  catch(e){ toast(e.message,"err"); }
}
async function setRole(id,role){
  try{ await api("POST",`/api/admin/users/${id}/role`,{role});
       toast("Role changed to "+role+".","ok"); load(); }
  catch(e){ toast(e.message,"err"); load(); }
}
async function del(id,email){
  if(!confirm("Delete "+email+"? This cannot be undone.")) return;
  try{ await api("DELETE",`/api/admin/users/${id}`); toast("Deleted.","ok"); load(); }
  catch(e){ toast(e.message,"err"); }
}
async function resetPw(id,email){
  const pw = prompt("New password for "+email+" (at least 12 characters).\\n"+
                    "They will be made to change it when they next sign in.");
  if(!pw) return;
  try{ await api("POST",`/api/admin/users/${id}/password`,{password:pw});
       toast("Password reset. Their current sessions were ended.","ok"); load(); }
  catch(e){ toast(e.message,"err"); }
}
// ── Kenyan PSC index ──────────────────────────────────────────────────
let PSC_POLL = null;

async function pscStatus(){
  const d = await api("GET","/api/admin/psc/status");
  const ix = d.index || {}, job = d.job || {};
  const el = document.getElementById("psc-status");
  if (ix.error) {
    el.innerHTML = `<span style="color:#f88">index unavailable: ${esc(ix.error)}</span>`;
  } else if (!ix.rows) {
    el.innerHTML = "Index is empty. Fetch a snapshot to build it.";
  } else {
    el.innerHTML = `<b>${ix.current}</b> current Kenyan PSCs across `
      + `<b>${ix.current_companies}</b> companies, from the snapshot of `
      + `<b>${esc(ix.snapshot_date)}</b>. `
      + `<span class="hint">${ix.ceased} further records are ceased and are `
      + `kept but not counted as current.</span>`;
  }
  const logEl = document.getElementById("psc-log");
  const btn = document.getElementById("psc-refresh");
  if (job.status === "running") {
    btn.disabled = true;
    btn.textContent = "Fetching…";
    logEl.style.display = "block";
    logEl.textContent = (job.log || []).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    if (!PSC_POLL) PSC_POLL = setInterval(pscStatus, 2000);
  } else {
    btn.disabled = false;
    btn.textContent = "Fetch latest snapshot";
    if (PSC_POLL) { clearInterval(PSC_POLL); PSC_POLL = null; }
    if (job.status === "failed") {
      logEl.style.display = "block";
      logEl.textContent = (job.log || []).join("\n");
    }
    if (job.status === "done") pscSearch();
  }
}

async function pscRefresh(){
  // Said plainly before it starts: this is a couple of gigabytes over the
  // server's connection and it REPLACES the table every search reads.
  if (!confirm("Download today's PSC snapshot from Companies House?\n\n"
             + "33 parts, roughly 2.4GB, several minutes. The index is "
             + "replaced with the new snapshot when it finishes, so it "
             + "always shows the register as it stands today.")) return;
  try {
    await api("POST","/api/admin/psc/refresh");
    toast("Fetching the snapshot…","ok");
    pscStatus();
  } catch(e){ toast(e.message,"err"); }
}

async function pscSearch(){
  const q = document.getElementById("psc-q").value.trim();
  const inc = document.getElementById("psc-ceased").checked ? "&ceased=1" : "";
  let d;
  try { d = await api("GET","/api/admin/psc/search?q="+encodeURIComponent(q)+inc); }
  catch(e){ return; }
  const rows = d.rows || [];
  const born = r => r.dob_year
    ? String(r.dob_month||"?").padStart(2,"0") + "/" + r.dob_year : "—";
  document.getElementById("psc-rows").innerHTML = rows.length
    ? rows.map(r => `<tr style="cursor:pointer${r.ceased_on ? ";opacity:.55" : ""}"
          onclick="pscOpen('${esc(r.company_number)}')">
        <td>${esc(r.company_number)}${r.company_name ? "<br><small>"+esc(r.company_name)+"</small>" : ""}</td>
        <td>${esc(r.psc_name)}${r.ceased_on ? '<br><small style="color:#f88">ceased '+esc(r.ceased_on)+"</small>" : ""}</td>
        <td><small>${born(r)}</small></td>
        <td>${esc(r.nationality)}</td>
        <td><small>${esc(r.country_of_residence||"")}</small></td>
        <td><small>${esc(r.natures_of_control||"")}</small></td>
        <td><small>${r.director_verdict ? esc(r.director_verdict) : "not checked"}</small></td>
      </tr>`).join("")
    : `<tr><td colspan="7" class="hint">No rows. ${q ? "Nothing matches that." : "Fetch a snapshot to build the index."}</td></tr>`;
}

async function pscOpen(cn){
  const el = document.getElementById("psc-detail");
  el.innerHTML = `<div class="hint">Asking Companies House about ${esc(cn)}…</div>`;
  let d;
  try { d = await api("GET","/api/admin/psc/company/"+encodeURIComponent(cn)); }
  catch(e){ el.innerHTML = `<div class="hint" style="color:#f88">${esc(e.message)}</div>`; return; }
  const c = d.company || {};
  const dr = d.director_rule || {};
  el.innerHTML = `
    <h3>${esc(c.name || cn)} <small>${esc(cn)}</small></h3>
    ${d.company_error ? `<div class="hint" style="color:#f88">Companies House: ${esc(d.company_error)}</div>` : ""}
    <div class="hint">
      ${esc(c.status||"")} ${c.type ? "· "+esc(c.type) : ""}
      ${c.incorporated ? "· incorporated "+esc(c.incorporated) : ""}
      ${(c.sic_codes||[]).length ? "· SIC "+esc((c.sic_codes||[]).join(", ")) : ""}
    </div>
    <div style="margin:8px 0;padding:8px;border-radius:4px;background:${dr.admissible ? "#13301c" : "#311"}">
      <b>Director rule:</b> ${dr.admissible ? "admissible" : "not admissible"}
      <br><small>${esc(dr.reason||"")}</small>
      <br><small class="hint">From /company/${esc(cn)}/officers &mdash; the PSC
      rows below are a different fact and do not decide this.</small>
    </div>
    <table><thead><tr><th>PSC</th><th>Born</th><th>Nationality</th>
      <th>Residence</th><th>Address</th><th>Control</th>
      <th>Notified</th><th>Ceased</th><th>ID verified</th></tr></thead><tbody>
      ${(d.psc_rows||[]).map(p => `<tr${p.ceased_on ? ' style="opacity:.55"' : ""}>
        <td>${esc(p.psc_name)}${p.surname ? "<br><small>"+esc([p.title,p.forename,p.middle_name,p.surname].filter(Boolean).join(" "))+"</small>" : ""}</td>
        <td><small>${p.dob_year ? esc(String(p.dob_month||"?").padStart(2,"0")+"/"+p.dob_year) : "—"}</small></td>
        <td>${esc(p.nationality)}</td>
        <td>${esc(p.country_of_residence||"")}</td>
        <td><small>${esc(p.address||"")}</small></td>
        <td><small>${esc(p.natures_of_control||"")}</small></td>
        <td><small>${esc(p.notified_on||"")}</small></td>
        <td><small${p.ceased_on ? ' style="color:#f88"' : ""}>${esc(p.ceased_on||"—")}</small></td>
        <td><small>${esc((p.identity_verified_on||"").split("T")[0] || "—")}</small></td>
      </tr>`).join("")}
    </tbody></table>`;
  pscSearch();
}

load();
pscStatus();
</script></body></html>"""


# ── The Kenyan PSC index ───────────────────────────────────────────────────
#
# Companies House cannot be searched by nationality; the free PSC snapshot
# can. psc_index holds the detail and the measurements - this is only the
# plumbing between it and the dashboard.
#
# The refresh runs in a thread with a progress log, like every other long
# job here, because it downloads 33 parts and nobody should hold a request
# open for that. One at a time: two concurrent rebuilds would race on the
# delete-older-snapshots step and could empty the table between them.

_psc_job: dict = {"status": "idle", "log": [], "summary": None}
_psc_lock = __import__("threading").Lock()


@admin.route("/api/admin/psc/status")
def api_psc_status():
    import psc_index
    return jsonify({"index": psc_index.status(),
                    "job": {"status": _psc_job["status"],
                            "log": _psc_job["log"][-40:],
                            "summary": _psc_job["summary"]}})


@admin.route("/api/admin/psc/refresh", methods=["POST"])
def api_psc_refresh():
    """Rebuild the index from today's snapshot. Superadmin only.

    Gated above admin because it replaces the table every company search
    will read, and because it pulls a couple of gigabytes over the server's
    connection. Approving a user and rebuilding the company universe are
    different kinds of act.
    """
    err = _require_superadmin()
    if err:
        return err
    import threading

    import psc_index

    with _psc_lock:
        if _psc_job["status"] == "running":
            return jsonify({"error": "a refresh is already running"}), 409
        _psc_job.update(status="running", log=[], summary=None)

    actor = _actor()
    max_parts = request.args.get("parts", type=int)   # a smaller first run

    def work():
        try:
            summary = psc_index.ingest(
                on_progress=lambda m: _psc_job["log"].append(m),
                max_parts=max_parts)
            _psc_job["summary"] = summary
            _psc_job["status"] = ("done" if summary.get("status") == "ok"
                                  else "failed")
            log.info("psc index refreshed by %s: %s", actor, summary)
        except Exception as e:                       # noqa: BLE001
            _psc_job["log"].append(f"failed: {type(e).__name__}: {e}")
            _psc_job["status"] = "failed"
            log.exception("psc index refresh failed")

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"status": "started"})


@admin.route("/api/admin/psc/search")
def api_psc_search():
    import psc_index
    inc = request.args.get("ceased", "").strip() in ("1", "true", "yes")
    return jsonify({"rows": psc_index.search(
        q=request.args.get("q", ""),
        limit=request.args.get("limit", default=50, type=int),
        offset=request.args.get("offset", default=0, type=int),
        include_ceased=inc)})


@admin.route("/api/admin/psc/company/<cn>")
def api_psc_company(cn):
    """Everything known about one company: the index rows, and live
    Companies House detail including whether the DIRECTOR rule is met.

    The index says a Kenyan person controls this company. Whether a Kenyan
    person DIRECTS it is a different question with a different source, and
    it is answered here rather than inferred, so the screen can show both
    and an operator can see when they disagree.
    """
    import api_v2
    import psc_index

    cn = api_v2._cn8(cn)
    out: dict = {"company_number": cn,
                 "psc_rows": [r for r in psc_index.search(q=cn, limit=50)
                              if r["company_number"] == cn]}
    try:
        from run_pipeline import ch_get
        profile = ch_get(f"/company/{cn}")
        out["company"] = {
            "name": profile.get("company_name"),
            "status": profile.get("company_status"),
            "type": profile.get("type"),
            "incorporated": profile.get("date_of_creation"),
            "sic_codes": profile.get("sic_codes") or [],
            "address": profile.get("registered_office_address") or {},
        }
    except Exception as e:
        out["company_error"] = str(e)

    ok, why = api_v2._fetch_kenyan(cn)
    out["director_rule"] = {"admissible": ok, "reason": why}
    try:
        psc_index.record_company_facts(
            cn,
            company_name=(out.get("company") or {}).get("name") or "",
            company_status=(out.get("company") or {}).get("status") or "",
            director_verdict=("admissible" if ok else why)[:200])
    except Exception:
        # Caching a name is a convenience. Failing to cache it must not
        # fail the lookup the operator actually asked for.
        pass
    return jsonify(out)


@admin.route("/admin")
def admin_page():
    return render_template_string(_PAGE)
