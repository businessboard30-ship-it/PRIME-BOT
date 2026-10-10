// Page CSS. BASE = the site look (tokens, header, buttons, framed cards, hero). SHELL = the app navigation and views.
// No style attributes anywhere: every rule lives here and is covered by the page nonce.
const BASE = `:root{--bg:#020407;--bg2:#06101a;--glow:rgba(150,210,255,.75);--glow-soft:rgba(120,190,255,.28);--line:rgba(225,243,255,.92);--faint:rgba(190,225,255,.38);--hair:rgba(190,225,255,.16);--text:#e4f1fb;--muted:rgba(196,222,242,.74);--ok:#7dffb0;--bad:#ff8f8f;
--mono:"Cascadia Mono","SF Mono",Consolas,"Liberation Mono",Menlo,ui-monospace,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--wrap:1180px;--hdr:64px}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:1.0625rem;line-height:1.6;overflow-x:hidden;min-height:100vh;display:flex;flex-direction:column}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse at 50% 0%,#0a1a2a 0%,rgba(2,4,7,0) 60%),radial-gradient(ellipse at 50% 110%,rgba(120,170,220,.16),transparent 60%)}
body::after{content:"";position:fixed;inset:0;z-index:60;pointer-events:none;opacity:.5;background:repeating-linear-gradient(0deg,rgba(255,255,255,.028) 0 1px,transparent 1px 3px)}
#bgfx{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;opacity:.55}
main,.site-footer,.site-header{position:relative;z-index:1}main{flex:1}
img,svg{max-width:100%}
a{color:#bfe3ff;text-decoration-color:rgba(150,210,255,.5);text-underline-offset:3px}a:hover{color:#fff}
:focus-visible{outline:2px solid #bfe3ff;outline-offset:3px;border-radius:2px}
.wrap{width:min(100% - 2.5rem,var(--wrap));margin-inline:auto}.narrow{width:min(100% - 2.5rem,780px);margin-inline:auto}
h1,h2,h3{font-family:var(--mono);letter-spacing:.06em;line-height:1.2;margin:0 0 .6em;text-shadow:0 0 12px var(--glow-soft);font-weight:600}
h2{font-size:1.15rem;text-transform:uppercase;letter-spacing:.09em}
p{margin:0 0 1em}.muted,.mut{color:var(--muted)}
.hidden,[hidden]{display:none!important}
.eyebrow{font-family:var(--mono);font-size:.78rem;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);margin:0 0 .9rem;display:flex;align-items:center;gap:.7rem}
.eyebrow::before{content:"";width:28px;height:1px;background:var(--line);box-shadow:0 0 6px var(--glow)}
code{font-family:var(--mono);font-size:.88em;background:rgba(150,210,255,.1);border:1px solid var(--hair);padding:.08rem .38rem;border-radius:2px;color:#e8f6ff}

/* header */
.site-header{position:sticky;top:0;z-index:50;height:var(--hdr);background:rgba(2,4,7,.72);backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);border-bottom:1px solid var(--hair)}
.site-header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent);opacity:.5}
.hdr-in{height:100%;display:flex;align-items:center;gap:1.2rem;justify-content:space-between}
.brand{font-family:var(--mono);font-weight:700;letter-spacing:.12em;font-size:.92rem;color:#fff;text-decoration:none;text-shadow:0 0 12px var(--glow);white-space:nowrap;display:flex;align-items:center;gap:.6rem}
.brand svg{width:26px;height:26px;flex:none}
.nav{display:flex;align-items:center;gap:.2rem}
.nav a{font-family:var(--mono);font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:.55rem .7rem}
.nav a:hover{color:#fff;text-shadow:0 0 10px var(--glow)}
@media (max-width:560px){.nav a.opt{display:none}}

/* buttons */
.btn{position:relative;display:inline-flex;align-items:center;justify-content:center;gap:.6rem;min-height:48px;padding:.8rem 1.5rem;font-family:var(--mono);font-size:.82rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600;text-decoration:none;color:#fff;background:rgba(6,16,26,.7);border:1px solid var(--line);overflow:hidden;cursor:pointer;box-shadow:0 0 14px var(--glow-soft),inset 0 0 14px rgba(150,210,255,.08);transition:box-shadow .25s,transform .25s,color .25s}
.btn::after{content:"";position:absolute;top:0;bottom:0;left:-60%;width:40%;background:linear-gradient(100deg,transparent,rgba(255,255,255,.55),transparent);transform:skewX(-20deg);transition:left .6s}
.btn:hover:not(:disabled){box-shadow:0 0 26px var(--glow),inset 0 0 18px rgba(150,210,255,.18);transform:translateY(-1px);color:#fff}
.btn:hover:not(:disabled)::after{left:130%}
.btn.primary{background:#eaf6ff;color:#02060a;border-color:#fff}
.btn.primary:hover:not(:disabled){color:#000;box-shadow:0 0 34px rgba(190,230,255,.9)}
.btn.ghost{background:transparent}.btn.sm{min-height:38px;padding:.4rem .9rem;font-size:.7rem}
.btn:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:.7rem;flex-wrap:wrap;align-items:center;margin:0 0 .8rem}.grow{flex:1 1 220px}

/* framed cards with corner brackets */
.card{position:relative;padding:1.5rem 1.4rem;margin:0 0 1.3rem;background:rgba(4,10,18,.62);border:1px solid var(--faint);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);transition:box-shadow .3s}
.card::before,.card::after{content:"";position:absolute;width:14px;height:14px;border:2px solid var(--line);transition:width .3s,height .3s}
.card::before{left:-2px;top:-2px;border-right:0;border-bottom:0}.card::after{right:-2px;bottom:-2px;border-left:0;border-top:0}
.card:hover{box-shadow:0 0 28px var(--glow-soft)}.card:hover::before,.card:hover::after{width:26px;height:26px}
.card p:last-child{margin-bottom:0}

/* form controls */
input,select,textarea{font-family:var(--mono);font-size:.92rem;color:#fff;background:rgba(4,10,18,.8);border:1px solid var(--faint);border-radius:0;padding:.7rem 1rem;min-height:46px;min-width:0;color-scheme:dark}
input.grow,textarea{width:100%}textarea{min-height:76px;resize:vertical}
input::placeholder,textarea::placeholder{color:rgba(196,222,242,.55)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--line);box-shadow:0 0 14px var(--glow-soft)}
select option{background:#06101a;color:#fff}

/* hero */
.hero{min-height:min(64svh,560px);display:grid;align-items:center;padding:2rem 0 2.2rem;overflow:hidden;position:relative}
.hero-stage{position:absolute;left:50%;top:46%;width:min(150vw,1100px);transform:translate(-50%,-50%);opacity:.5;pointer-events:none;-webkit-mask-image:radial-gradient(closest-side,#000 55%,transparent 100%);mask-image:radial-gradient(closest-side,#000 55%,transparent 100%)}
.hero-stage svg{display:block;width:100%;height:auto;overflow:visible;filter:drop-shadow(0 0 10px rgba(120,190,255,.28))}
.glow{filter:drop-shadow(0 0 4px var(--glow)) drop-shadow(0 0 10px rgba(120,190,255,.55))}
.spin{transform-origin:380px 200px;animation:spin var(--s,30s) linear infinite}.spin.rev{animation-direction:reverse}
@keyframes spin{to{transform:rotate(360deg)}}
.pulse{animation:pulse 2.2s ease-in-out infinite;transform-origin:380px 200px}
@keyframes pulse{50%{opacity:.35;transform:scale(.9)}}
.s6{--s:60s}.s28{--s:28s}.s17{--s:17s}.s11{--s:11s}.s35{--s:35s}
.hero-in{position:relative;z-index:2;text-align:center;display:flex;flex-direction:column;align-items:center}
.hud-tag{align-self:flex-start;position:relative;padding:1rem 1.4rem .8rem;margin-bottom:clamp(1rem,4vh,2.4rem);font-family:var(--mono);font-size:clamp(.7rem,2vw,.86rem);letter-spacing:.07em;text-shadow:0 0 10px var(--glow);text-align:left}
.hud-tag svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.hud-tag svg *{fill:none;stroke:var(--line);stroke-width:1.2;vector-effect:non-scaling-stroke;filter:drop-shadow(0 0 3px var(--glow))}
.hud-tag b{font-weight:600}
.hero h1{font-size:clamp(2.2rem,8vw,4.8rem);letter-spacing:.1em;margin:0 0 1rem;text-transform:uppercase;text-shadow:0 0 22px var(--glow),0 0 2px #fff}
.hero .sub{font-family:var(--mono);font-size:clamp(.74rem,1.8vw,.95rem);letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:1rem}
.hero .vp{font-size:clamp(1.02rem,2.1vw,1.25rem);max-width:46ch;margin:0 auto;color:var(--text)}
.in-app .hero{min-height:0;padding:1.4rem 0 .4rem}.in-app .hero .vp,.in-app .hud-tag{display:none}.in-app .hero h1{font-size:clamp(1.6rem,5vw,2.6rem);margin-bottom:.4rem}.in-app .hero .sub{margin-bottom:0}
#msg{min-height:1.5em;font-family:var(--mono);font-size:.82rem;letter-spacing:.06em;margin:.3rem 0 1rem;text-align:center}
.ok{color:var(--ok)}.bad{color:var(--bad)}

/* cracking white paint */
.crack{position:relative;display:inline-block}
.crack .shards{position:absolute;inset:-.12em -.2em;pointer-events:none;z-index:3}
.crack .shards i{position:absolute;inset:0;background:linear-gradient(135deg,#fff,#cfe9ff);will-change:transform,opacity;transform-origin:50% 0}
.crack .seam{position:absolute;left:-2%;right:-2%;top:50%;height:2px;background:#000;z-index:4;pointer-events:none;box-shadow:0 0 14px #fff;transform:scaleX(0);transform-origin:50% 50%}
.crack.in .seam{animation:seam .9s ease-out forwards}
@keyframes seam{0%{transform:scaleX(0);opacity:1}55%{transform:scaleX(1);opacity:1}100%{transform:scaleX(1);opacity:0}}
.crack.in .shards i{animation:fall 1.1s cubic-bezier(.5,.1,.3,1) forwards;animation-delay:calc(.45s + var(--d)*.07s)}
@keyframes fall{0%{transform:translate(0,0) rotate(0);opacity:1}25%{opacity:1}100%{transform:translate(var(--tx),var(--ty)) rotate(var(--r));opacity:0}}
/* scroll reveal */
.js .reveal{opacity:0;transform:translateY(22px);transition:opacity .8s ease,transform .8s ease;transition-delay:calc(var(--i,0)*70ms)}
.js .reveal.in{opacity:1;transform:none}

/* crons list */
.job{border-top:1px solid var(--hair);padding:.9rem 0}.job:first-child{border-top:0;padding-top:0}
.name{font-family:var(--mono);font-weight:600;letter-spacing:.04em}
.url{word-break:break-all;font-family:var(--mono);font-size:.84rem;margin:.2rem 0}
.badge{display:inline-block;font-family:var(--mono);font-size:.66rem;letter-spacing:.14em;text-transform:uppercase;padding:.18rem .5rem;border:1px solid var(--faint);color:var(--text);vertical-align:middle;margin-left:.4rem}
.badge.prem{border-color:var(--line);box-shadow:0 0 8px var(--glow-soft)}
.who{font-family:var(--mono);font-size:.84rem;letter-spacing:.06em}
.fine{font-size:.9rem}
.tsbox{min-height:65px}

/* footer */
.site-footer{border-top:1px solid var(--hair);background:rgba(2,4,7,.85);padding:1.6rem 0;margin-top:2rem;font-family:var(--mono);font-size:.74rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.site-footer .wrap{display:flex;flex-wrap:wrap;gap:.6rem 1.6rem;justify-content:space-between}
.site-footer a{color:var(--text);text-decoration:none}.site-footer a:hover{text-shadow:0 0 10px var(--glow)}

/* motion preferences / low power */
.no-motion .spin,.no-motion .pulse{animation:none}.no-motion .btn::after{display:none}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}.spin,.pulse,.crack .seam{animation:none!important}.btn::after{display:none}.js .reveal{opacity:1;transform:none;transition:none}.crack .shards,.crack .seam{display:none}}
@media (pointer:fine) and (hover:hover){.par{will-change:transform}}
`;

const SHELL = `
/* ===== app shell: sidebar, views, dashboard ===== */
.out-wrap{max-width:780px;margin-inline:auto}
.menu-btn{display:none;width:44px;height:44px;flex:none;position:relative;background:rgba(6,16,26,.7);border:1px solid var(--faint);cursor:pointer;padding:0}
.menu-btn span{position:absolute;left:11px;right:11px;height:2px;background:var(--line);box-shadow:0 0 6px var(--glow);transition:transform .25s,opacity .25s}
.menu-btn span:nth-child(1){top:14px}.menu-btn span:nth-child(2){top:21px}.menu-btn span:nth-child(3){top:28px}
body.menu-open .menu-btn span:nth-child(1){transform:translateY(7px) rotate(45deg)}
body.menu-open .menu-btn span:nth-child(2){opacity:0}
body.menu-open .menu-btn span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.scrim{position:fixed;inset:0;z-index:54;background:rgba(2,4,7,.66);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}

.shell{display:grid;grid-template-columns:260px minmax(0,1fr);gap:1.6rem;align-items:start;padding-bottom:1rem}
.side{position:sticky;top:calc(var(--hdr) + 1rem);margin:0;padding:1.1rem .9rem}
.side-tag{display:flex;align-items:center;gap:.55rem;font-family:var(--mono);font-size:.68rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin:0 0 .9rem .3rem}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok);flex:none}
.dot.live{animation:blink 2.4s ease-in-out infinite}
.dot.bad{background:var(--bad);box-shadow:0 0 8px var(--bad)}.dot.idle{background:var(--faint);box-shadow:none}
@keyframes blink{50%{opacity:.35}}
.side-nav{display:flex;flex-direction:column;gap:.3rem}
.side-nav a{position:relative;display:flex;align-items:center;gap:.8rem;min-height:46px;padding:.6rem .8rem;font-family:var(--mono);font-size:.76rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);text-decoration:none;border:1px solid transparent;transition:color .2s,background .2s,border-color .2s,box-shadow .2s}
.side-nav a svg{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:square}
.side-nav a:hover{color:#fff;background:rgba(150,210,255,.06);border-color:var(--hair)}
.side-nav a[aria-current="page"]{color:#fff;background:rgba(150,210,255,.1);border-color:var(--faint);box-shadow:0 0 18px var(--glow-soft),inset 0 0 14px rgba(150,210,255,.06)}
.side-nav a[aria-current="page"]::before{content:"";position:absolute;left:-1px;top:6px;bottom:6px;width:3px;background:var(--line);box-shadow:0 0 10px var(--glow)}
.side-nav a[aria-current="page"] svg{filter:drop-shadow(0 0 4px var(--glow))}
.side-nav .grow2{flex:1}
.count,.chip{margin-left:auto;font-style:normal;font-size:.62rem;letter-spacing:.14em;padding:.12rem .45rem;border:1px solid var(--faint);color:var(--text)}
.count{min-width:1.7rem;text-align:center}
.chip.soon{color:var(--muted);border-style:dashed}.chip.on{color:var(--ok);border-color:rgba(125,255,176,.45)}
.side-foot{margin-top:1.1rem;padding-top:1rem;border-top:1px solid var(--hair)}
.side-who{font-family:var(--mono);font-size:.8rem;letter-spacing:.06em;margin:0 0 .15rem .3rem;word-break:break-all}
.side-plan{font-family:var(--mono);font-size:.68rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin:0 0 .6rem .3rem}

/* segmented plan meter */
.meter{display:grid;grid-template-columns:repeat(20,1fr);gap:3px;height:14px}
.meter i{display:block;background:rgba(150,210,255,.1);border:1px solid var(--hair);transition:background .4s,box-shadow .4s}
.meter i.on{background:var(--line);border-color:var(--line);box-shadow:0 0 8px var(--glow)}
.meter.max i.on{animation:sweep 2.6s ease-in-out infinite;animation-delay:calc(var(--n)*70ms)}
@keyframes sweep{50%{opacity:.4}}
.meter.big{height:22px;gap:4px}

/* views */
.view{min-width:0;animation:vin .45s ease both}
@keyframes vin{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.vhead{position:relative;margin:0 0 1.6rem;padding:0 0 1.1rem;scroll-margin-top:calc(var(--hdr) + 12px)}
.vhead::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;background:linear-gradient(90deg,var(--line),var(--hair) 55%,transparent);box-shadow:0 0 8px var(--glow-soft)}
.vhead h2{font-size:clamp(1.4rem,4vw,2rem);letter-spacing:.1em;margin:0 0 .4rem;text-shadow:0 0 18px var(--glow)}
.vhead h2:focus{outline:none}
.vhead p{margin:0}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1.3rem}
.grid2 .card{margin:0}
.actions{display:flex;gap:.7rem;flex-wrap:wrap}
.bigline{font-family:var(--mono);font-size:clamp(1.05rem,3vw,1.5rem);letter-spacing:.06em;margin:0 0 .3rem;text-shadow:0 0 14px var(--glow-soft);word-break:break-all}
.bigline small{display:block;font-size:.72rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:.35rem;text-shadow:none}
.nums{display:flex;align-items:baseline;gap:.5rem;margin:.2rem 0 .9rem;font-family:var(--mono)}
.nums b{font-size:2.4rem;line-height:1;text-shadow:0 0 18px var(--glow)}.nums span{color:var(--muted);letter-spacing:.1em}
.mods{list-style:none;margin:0;padding:0;display:grid;gap:.1rem}
.mods li{display:flex;align-items:center;gap:.8rem;padding:.7rem 0;border-top:1px solid var(--hair);font-family:var(--mono);font-size:.82rem;letter-spacing:.08em}
.mods li:first-child{border-top:0;padding-top:0}.mods li:last-child{padding-bottom:0}
.mods .why{display:block;color:var(--muted);font-family:var(--sans);letter-spacing:0;font-size:.86rem}

/* placeholder wireframes */
.ph{position:relative;min-height:230px;display:grid;place-items:center;text-align:center;padding:1.2rem;border:1px dashed var(--faint);background:repeating-linear-gradient(135deg,rgba(150,210,255,.04) 0 8px,transparent 8px 16px)}
.ph svg{position:absolute;inset:0;width:100%;height:100%;opacity:.55}
.ph svg *{fill:none;stroke:var(--line);stroke-width:1.2;vector-effect:non-scaling-stroke}
.ph .grid{stroke:var(--hair);stroke-dasharray:4 7}
.ph .trace{stroke-dasharray:10 8;animation:flow 3s linear infinite;filter:drop-shadow(0 0 4px var(--glow))}
@keyframes flow{to{stroke-dashoffset:-72}}
.ph-msg{position:relative;z-index:1;background:rgba(2,4,7,.82);border:1px solid var(--faint);padding:.8rem 1.1rem;font-family:var(--mono);font-size:.78rem;letter-spacing:.14em;text-transform:uppercase}
.ph-msg small{display:block;margin-top:.3rem;color:var(--muted);letter-spacing:.04em;text-transform:none;font-family:var(--sans);font-size:.88rem}
.ticks{list-style:none;margin:0;padding:0}
.ticks li{position:relative;padding:.3rem 0 .3rem 1.5rem}
.ticks li::before{content:"";position:absolute;left:.2rem;top:.85rem;width:7px;height:7px;border:1px solid var(--line);transform:rotate(45deg)}

.card.danger{border-color:rgba(255,143,143,.5)}
.card.danger::before,.card.danger::after{border-color:var(--bad)}
.job .name .dot{margin-right:.55rem;vertical-align:middle}

@media (max-width:900px){
  body.in-app .menu-btn{display:block}
  .shell{grid-template-columns:minmax(0,1fr)}
  .side{display:none}
  body.menu-open .side{display:block;position:fixed;z-index:55;left:.75rem;right:.75rem;top:calc(var(--hdr) + .6rem);max-height:calc(100svh - var(--hdr) - 1.4rem);overflow:auto;background:rgba(2,6,12,.96)}
  .grid2{grid-template-columns:minmax(0,1fr)}
}
@media (min-width:901px){.scrim{display:none!important}}
@media (prefers-reduced-motion:reduce){.view{animation:none}.dot.live,.meter.max i.on,.ph .trace{animation:none}}
`;

const TILES = `
/* dashboard count tiles */
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem;margin:0 0 1.3rem}
.tile{position:relative;display:flex;flex-direction:column;gap:.35rem;padding:1.1rem 1.1rem 1rem;text-decoration:none;color:var(--text);background:rgba(4,10,18,.62);border:1px solid var(--faint);overflow:hidden;transition:box-shadow .3s,transform .3s,border-color .3s}
.tile::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:var(--line);box-shadow:0 0 10px var(--glow);opacity:.7}
.tile:hover{box-shadow:0 0 26px var(--glow-soft);transform:translateY(-2px);border-color:var(--line);color:#fff}
.tile-n{font-family:var(--mono);font-size:clamp(1.8rem,5vw,2.6rem);font-weight:700;line-height:1;text-shadow:0 0 18px var(--glow)}
.tile-l{font-family:var(--mono);font-size:.7rem;letter-spacing:.2em;text-transform:uppercase;color:var(--muted)}
.tile.ok .tile-n{color:var(--ok);text-shadow:0 0 18px rgba(125,255,176,.45)}
.tile.ok::before{background:var(--ok);box-shadow:0 0 10px var(--ok)}
.tile.bad::before{background:var(--bad);box-shadow:0 0 10px var(--bad)}
.tile.bad .tile-n{color:var(--bad);text-shadow:0 0 18px rgba(255,143,143,.5)}
@media (max-width:700px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
`;

const HIST = `
/* run history */
.hist{margin:.2rem 0 .4rem;padding:.7rem .8rem;border:1px solid var(--hair);background:rgba(2,6,12,.55)}
.hist-scroll{max-height:260px;overflow:auto}
.hrow{display:grid;grid-template-columns:10px minmax(0,1fr) 5.5rem 5rem 3.4rem;gap:.7rem;align-items:center;padding:.35rem 0;border-top:1px solid var(--hair);font-family:var(--mono);font-size:.76rem;letter-spacing:.04em}
.hrow:first-child{border-top:0}
.hdot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok)}
.hrow.bad .hdot{background:var(--bad);box-shadow:0 0 6px var(--bad)}
.hrow .hs,.hrow .hm{text-align:right;color:var(--muted)}.hrow .hk{text-align:right;font-weight:700}
.hrow.ok .hk{color:var(--ok)}.hrow.bad .hk{color:var(--bad)}
@media (max-width:560px){.hrow{grid-template-columns:10px minmax(0,1fr) 3.6rem 3rem}.hrow .hm{display:none}}
`;

const STATS = `
/* statistics */
.seg-btn[aria-pressed="true"]{background:#eaf6ff;color:#02060a;border-color:#fff;box-shadow:0 0 18px rgba(190,230,255,.6)}
.seg-btn .chip{margin-left:.4rem;font-size:.55rem}
.seg-btn[aria-pressed="true"] .chip{color:#02060a;border-color:#02060a}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart .cg{fill:none;stroke:var(--hair);stroke-dasharray:4 6}
.chart .ca{fill:none;stroke:var(--faint)}
.chart .ct{fill:var(--muted);font-family:var(--mono);font-size:11px;letter-spacing:.04em}
.chart .cl{fill:none;stroke:var(--line);stroke-width:2.2;stroke-linejoin:round;stroke-linecap:round;filter:drop-shadow(0 0 5px var(--glow))}
.chart .cf{fill:rgba(150,210,255,.1);stroke:none}
.chart .cd{fill:#fff;stroke:var(--bg);stroke-width:1.5}
.chart .b-ok{fill:var(--ok);opacity:.85}.chart .b-bad{fill:var(--bad)}
.chart .cn{fill:var(--muted);font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-anchor:middle;text-transform:uppercase}
.up{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:.2rem .9rem;padding:.85rem 0;border-top:1px solid var(--hair)}
.up:first-child{border-top:0;padding-top:0}
.up .un{font-family:var(--mono);font-weight:600;letter-spacing:.04em;word-break:break-word}
.up .up-pct{font-family:var(--mono);font-weight:700;text-align:right}
.up .up-sub{grid-column:1/-1;font-size:.84rem;color:var(--muted);margin-bottom:.3rem}
.up .meter{grid-column:1/-1;height:10px}
.up.good .up-pct{color:var(--ok)}.up.good .meter i.on{background:var(--ok);border-color:var(--ok);box-shadow:0 0 8px var(--ok)}
.up.low .up-pct{color:var(--bad)}.up.low .meter i.on{background:var(--bad);border-color:var(--bad);box-shadow:0 0 8px var(--bad)}
`;

export const CSS = BASE + SHELL + TILES + HIST + STATS;
