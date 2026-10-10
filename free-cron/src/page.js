// The single page, assembled from three parts in ./ui: css.js (look), markup.js (structure and the five views), script.js (behaviour).
// Strict CSP: one nonce for every script and the style block, no inline style attributes, no inline handlers, no innerHTML anywhere.
// Look and feel match the PRIME BOT website (website/assets/site.css): same tokens, fonts, HUD hero, background network and scroll animations.
import { CSS } from "./ui/css.js";
import { MARKUP } from "./ui/markup.js";
import { appScript } from "./ui/script.js";

const esc = (s) => String(s).replace(/[^A-Za-z0-9_.-]/g, "");

export function pageHtml(nonce, siteKey) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Free Cron: 5 free scheduled URL calls</title>
<meta name="description" content="Call any public URL on a schedule. Sign in with GitHub, get 5 free crons. Premium is unlimited.">
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#020407">
<script nonce="${nonce}">document.documentElement.classList.add("js")</script>
<style nonce="${nonce}">
${CSS}</style></head><body>
${MARKUP}
<script nonce="${nonce}" src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
<script nonce="${nonce}">
${appScript(esc(siteKey))}
</script></body></html>`;
}
