# PRIME BOT × WELCOME BOT — marketing website

Static site for Cloudflare Pages. Plain HTML, CSS and vanilla JS: no framework, no build step, no third-party scripts, fonts or CDNs. Deployed by `.github/workflows/deploy-website.yml` to the Pages project `prime-bot-site`.

Do not confuse it with `captcha-pages/`, `redirector/`, `unlock-pages/` or the Next.js `app/`: this folder is independent and does not touch them.

## One-time setup

1. **Secrets.** In GitHub: Settings → Secrets and variables → Actions. The same two secrets used by the captcha-pages workflow work here:
   - `CLOUDFLARE_API_TOKEN` (needs *Cloudflare Pages: Edit*)
   - `CLOUDFLARE_ACCOUNT_ID`
2. **Project.** The workflow creates the Pages project `prime-bot-site` if it does not exist. To use another name, set the repository variable `CF_PAGES_WEBSITE_PROJECT`.
3. **Deploy.** Merging to `main` publishes production. Pull requests that touch `website/` publish a preview deployment. You can also run the workflow manually (Actions → Deploy website → Run workflow).
4. **Custom domain.** Cloudflare dashboard → Workers & Pages → `prime-bot-site` → Custom domains → Set up a domain. Then switch every absolute URL from the default `https://prime-bot-site.pages.dev` to your domain:

   ```bash
   cd website && ./set-domain.sh example.com
   ```

   This rewrites canonical URLs, Open Graph tags, JSON-LD, `sitemap.xml` and `robots.txt`. Commit the result.

## Fill these placeholders

| Placeholder | Where | Notes |
|---|---|---|
| `[BOT_INVITE_URL]` | `assets/config.js` → `BOT_INVITE_URL` | One constant used by every “Add to Discord” button. Prefilled from the application ID in `config.py`; verify it. |
| `[DOMAIN]` | `./set-domain.sh <domain>` | See above. |
| `[TOPGG_URL]` | `assets/config.js` → `TOPGG_URL` | Optional. Empty hides every Top.gg link. |
| `[STATS_API_URL]` | `assets/config.js` → `STATS_API_URL` | Optional JSON `{"servers":123,"commands":87}`. Empty keeps the stats strip hidden. If you set it, add its origin to `connect-src` in `_headers`. |
| Contact email | `assets/config.js` and the generated pages | Currently `maxwelldumenya5@outlook.com`. A domain email looks better to payment reviewers. |

## Local preview

```bash
cd website && python3 -m http.server 8080
```

`_headers` and `_redirects` are applied only by Cloudflare Pages; the plain static server ignores them.

## Layout

- `index.html`, `features/`, `pricing/`, `premium/`, `commands/`, `docs/`, `support/`, `status/`, `terms/`, `privacy/`, `refund/`, `about/`, `404.html`
- `assets/site.css`, `assets/site.js`, `assets/config.js`, `assets/boot.js`
- `_headers` (security headers and CSP), `_redirects`, `sitemap.xml`, `robots.txt`, `site.webmanifest`, favicons, `og.png`, `og.svg`

## Content rules

Every price, perk and policy comes from the repo: `config.py` (`*_FEE_USD`, `XP_BOOST_BUNDLES`, `XP_SERVER_BOOST_TIERS`, `PREMIUM_*`), `api/legal_pages.py`, `discord_bot/cogs/_views_premium.py`, `discord_bot/cogs/help.py` and `README.md`. If prices change in `config.py`, update the pages in `website/` too. The site has no invented statistics, reviews or partner logos; keep it that way.
