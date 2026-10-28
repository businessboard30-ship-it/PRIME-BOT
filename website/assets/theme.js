(function(){
"use strict";
var KEY="prime-theme",THEMES=[["ice","Ice"],["violet","Violet"],["emerald","Emerald"],["sunset","Sunset"],["rose","Rose"],["gold","Gold"]];
function get(){try{return localStorage.getItem(KEY)||"ice"}catch(e){return"ice"}}
function apply(t){var d=document.documentElement;if(t==="ice")d.removeAttribute("data-theme");else d.setAttribute("data-theme",t)}
function save(t){try{localStorage.setItem(KEY,t)}catch(e){}}
var cur=get();apply(cur);
function build(){
  if(document.querySelector(".theme-fab"))return;
  var box=document.createElement("div");box.className="theme-fab";
  var btn=document.createElement("button");btn.type="button";btn.className="theme-fab-btn";btn.setAttribute("aria-label","Change site colours");btn.setAttribute("aria-expanded","false");
  btn.innerHTML='<svg class="theme-fab-rings" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle class="ring ring-outer" cx="24" cy="24" r="19"/><circle class="ring ring-mid" cx="24" cy="24" r="13.5"/><circle class="ring ring-inner" cx="24" cy="24" r="7.5"/><circle class="ring-core" cx="24" cy="24" r="2"/></svg>';
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
  var style=document.createElement("style");style.textContent='.theme-fab-btn{position:relative;overflow:hidden;border-color:rgba(145,205,255,.42);background:radial-gradient(circle at 50% 50%,rgba(125,205,255,.13),rgba(4,10,18,.92) 70%);box-shadow:0 0 0 1px rgba(145,205,255,.06),0 0 18px rgba(125,190,255,.16)}.theme-fab-btn:hover,.theme-fab-btn:focus-visible{border-color:rgba(175,225,255,.8);box-shadow:0 0 0 3px rgba(125,205,255,.08),0 0 25px rgba(125,190,255,.28)}.theme-fab-rings{width:31px;height:31px;overflow:visible;filter:drop-shadow(0 0 3px rgba(125,205,255,.24));transition:transform .35s ease}.theme-fab-btn:hover .theme-fab-rings{transform:rotate(18deg) scale(1.04)}.theme-fab-rings .ring{fill:none;stroke:#a9dcff;stroke-width:1.1;opacity:.8}.theme-fab-rings .ring-outer{stroke-dasharray:2.5 2.8;opacity:.8}.theme-fab-rings .ring-mid{stroke:#dcefff;stroke-dasharray:18 4;opacity:.95}.theme-fab-rings .ring-inner{stroke:#7fd4ff;stroke-width:1.35}.theme-fab-rings .ring-core{fill:#dff5ff;stroke:#7fd4ff;stroke-width:1}.theme-fab.open .theme-fab-rings{transform:rotate(45deg)}@media(prefers-reduced-motion:reduce){.theme-fab-rings{transition:none!important}}';
  document.head.appendChild(style);
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",build);else build();
})();