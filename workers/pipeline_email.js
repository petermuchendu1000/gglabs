/**
 * pipeline-email - the Cloudflare Email Worker that receives dev@<domain>.
 *
 * WHY A WORKER AND NOT A FORWARD. Cloudflare will not deliver to a
 * destination address no human has verified by clicking a link inside it.
 * The pipeline assigns a DIFFERENT Gmail per company out of emails.xlsx, so a
 * forward-based route costs one manual verification per company - precisely
 * the kind of step this pipeline exists to remove. A worker-action routing
 * rule needs no verification of the recipient domain at all.
 *
 * The forward THIS worker performs is a different thing: it goes to the one
 * address that is already verified on the account, so it is a constant, not
 * a per-company step. That address is supplied as the FORWARD_TO binding and
 * is deliberately absent from this file.
 *
 * WHAT WAS MEASURED BEFORE RELYING ON ANY OF THIS (2026-11-17, a real
 * delivery to rawtest@simon-mwangi.site):
 *   - fetch() works inside email(). Undocumented, and the whole route rested
 *     on it: GET to the pipeline's own /health returned 200 in 658ms.
 *   - message.raw can be drained to completion (7106 of 7106 bytes) and a
 *     subrequest still issued afterwards in the same handler.
 *   - KV bindings work inside email().
 *
 * WHAT IS STILL NOT KNOWN, and why this file is as small as it is:
 *   - Cloudflare documents NOTHING about what happens to a message when an
 *     email() handler throws or times out. So this handler does not catch and
 *     swallow: a failed forward is re-thrown. A bounce to the sender is
 *     recoverable; a message silently dropped is not, and the message this
 *     route exists to carry is a Play Console verification code that arrives
 *     once.
 *   - Google's real verification email has never been seen here, so there is
 *     NO code-extraction parser in this file. Writing one against a guessed
 *     layout would produce a parser that looks like it works. When a real
 *     message has been captured, parse the MIME and search ONLY text/plain:
 *     scanning raw MIME for 4-8 digit runs was tried and returns the sender's
 *     IPv6 fragments (2607, 4864, ...) out of the Received headers.
 */

export default {
  async email(message, env, ctx) {
    const to = message.to;
    const from = message.from;

    // A missing binding must fail loudly. A default would silently route
    // every company's verification mail to the wrong place, or nowhere.
    const dest = (env && env.FORWARD_TO) || "";
    if (!dest) {
      console.log(`pipeline-email: NO FORWARD_TO binding; to=${to} from=${from}`);
      throw new Error(
        "pipeline-email has no FORWARD_TO binding, so there is nowhere to " +
        "deliver this message. Set it to an address verified on this " +
        "Cloudflare account."
      );
    }

    console.log(`pipeline-email: to=${to} from=${from} -> ${dest}`);
    // Not wrapped in try/catch on purpose - see the header. Cloudflare's
    // behaviour on a thrown email() handler is undocumented, and an
    // undocumented bounce beats a documented silent drop.
    await message.forward(dest);
    console.log(`pipeline-email: forwarded to=${to}`);
  },
};
