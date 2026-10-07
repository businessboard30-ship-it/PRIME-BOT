# Cloudflare Turnstile verification page

Static page for the `/setupverification` **Captcha** mode. Members get a signed link
from the bot, solve Cloudflare Turnstile here, the backend (`/api/verify_captcha`)
validates it, and the bot swaps their roles automatically.

## One-time setup
1. Cloudflare dashboard → **Turnstile** → Add widget (mode: Managed). Hostname = your Pages domain
   (e.g. `prime-bot-verify.pages.dev`). Copy the **site key** and **secret key**.
2. Put the site key in `captcha-pages/config.json` (`turnstile_site_key`; it is public). Check `api_base` is your Railway URL.
3. Cloudflare → Workers & Pages → create a Pages project named `prime-bot-verify` (Direct Upload), or let the
   GitHub Action create it on first deploy. Add repo secrets `CLOUDFLARE_API_TOKEN` (Pages: Edit) and
   `CLOUDFLARE_ACCOUNT_ID`; push to `main` (or run the workflow manually).
4. Railway env vars on the bot **and** API service:
   - `TURNSTILE_SECRET_KEY` = widget secret key
   - `TURNSTILE_PAGES_URL` = `https://prime-bot-verify.pages.dev`
   - `VERIFY_SIGNING_SECRET` = long random string (`openssl rand -hex 32`)
5. Redeploy. Until all three are set, captcha mode keeps using the old in-Discord math question.
