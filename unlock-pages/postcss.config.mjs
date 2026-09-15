/** @type {import('postcss-load-config').Config} */
// Plain CSS only in this standalone build — no Tailwind. An explicit
// (empty-plugins) config here stops postcss-load-config from walking up
// and picking up the parent PRIME-BOT repo's Tailwind postcss config.
const config = {
  plugins: {},
}

export default config
