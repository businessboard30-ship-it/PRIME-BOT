(function(){
"use strict";
var KEY="prime-theme",THEMES=[["ice","Ice"],["violet","Violet"],["emerald","Emerald"],["sunset","Sunset"],["rose","Rose"],["gold","Gold"]];
function get(){try{return localStorage.getItem(KEY)||"emerald"}catch(e){return"emerald"}}
function apply(t){var d=document.documentElement;if(t==="ice")d.removeAttribute("data-theme");else d.setAttribute("data-theme",t)}
function save(t){try{localStorage.setItem(KEY,t)}catch(e){}}
var cur=get();apply(cur);
function build(){
  if(document.querySelector(".theme-fab"))return;
  var box=document.createElement("div");box.className="theme-fab";
  var btn=document.createElement("button");btn.type="button";btn.className="theme-fab-btn";btn.setAttribute("aria-label","Change site colours");btn.setAttribute("aria-expanded","false");
  btn.innerHTML='<svg class="theme-fab-rings" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle class="ring ring-outer" cx="24" cy="24" r="19"/><circle class="ring ring-mid" cx="24" cy="24" r="13.5"/><circle class="ring ring-inner" cx="24" cy="24" r="7.5"/><circle class="ring-core" cx="24" cy="24" r="5.5"/></svg>';
  var menu=document.createElement("div");menu.className="theme-fab-menu";menu.setAttribute("role","group");menu.setAttribute("aria-label","Colour theme");
  THEMES.forEach(function(t){
    var o=document.createElement("button");o.type="button";o.className="theme-opt";o.setAttribute("data-theme-pick",t[0]);
    var dot=document.createElement("span");dot.className="theme-dot t-"+t[0];o.appendChild(dot);o.appendChild(document.createTextNode(t[1]));
    o.addEventListener("click",function(){cur=t[0];apply(cur);save(cur);mark()});
    menu.appendChild(o);
  });
  function mark(){var b=menu.querySelectorAll(".theme-opt");for(var i=0;i<b.length;i++)b[i].setAttribute("aria-pressed",b[i].getAttribute("data-theme-pick")===cur?"true":"false")}
  function setOpen(v){box.classList.toggle("open",v);btn.setAttribute("aria-expanded",v?"true":"false")}
  btn.addEventListener("click",function(e){e.stopPropagation();setOpen(!box.classList.contains("open"))});
  document.addEventListener("click",function(e){if(!box.contains(e.target))setOpen(false)});
  document.addEventListener("keydown",function(e){if(e.key==="Escape")setOpen(false)});
  box.appendChild(menu);box.appendChild(btn);document.body.appendChild(box);mark();
  var style=document.createElement("style");style.textContent='.theme-fab-btn{position:relative;overflow:hidden;border:1px solid rgba(93,240,176,.72);background:radial-gradient(circle at 50% 42%,#164b37 0%,#09291f 58%,#06140f 100%) !important;color:#dffff0;box-shadow:0 0 0 1px rgba(93,240,176,.12),0 0 18px rgba(55,220,145,.2)}.theme-fab-btn:hover,.theme-fab-btn:focus-visible{border-color:#8bffca;background:radial-gradient(circle at 50% 42%,#216747 0%,#0b3526 58%,#071a12 100%) !important;box-shadow:0 0 0 3px rgba(93,240,176,.12),0 0 25px rgba(55,220,145,.34);outline:none}.theme-fab-rings{width:31px;height:31px;overflow:visible;filter:drop-shadow(0 0 3px rgba(93,240,176,.3));transition:transform .35s ease}.theme-fab-btn:hover .theme-fab-rings{transform:rotate(18deg) scale(1.04)}.theme-fab-rings .ring{fill:none;stroke:#a7ffd5;stroke-width:1.1;opacity:.88}.theme-fab-rings .ring-outer{stroke:#5df0b0;stroke-dasharray:2.5 2.8;opacity:.95}.theme-fab-rings .ring-mid{stroke:#e0fff0;stroke-dasharray:18 4;opacity:.95}.theme-fab-rings .ring-inner{stroke:#5df0b0;stroke-width:1.5}.theme-fab-rings .ring-core{fill:#5df0b0;fill-opacity:.95;stroke:#e0fff0;stroke-width:1;filter:drop-shadow(0 0 4px rgba(93,240,176,.95))}.theme-fab.open .theme-fab-rings{transform:rotate(45deg)}@media(prefers-reduced-motion:reduce){.theme-fab-rings{transition:none!important}}';
  document.head.appendChild(style);
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",build);else build();
})();