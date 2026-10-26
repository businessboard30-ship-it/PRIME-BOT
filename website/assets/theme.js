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
  var btn=document.createElement("button");btn.type="button";btn.className="theme-fab-btn";btn.setAttribute("aria-label","Change site colours");btn.setAttribute("aria-expanded","false");btn.textContent="\uD83C\uDFA8";
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
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",build);else build();
})();
