(function(){
"use strict";
var C=window.SITE_CONFIG||{};
var $=function(s,r){return (r||document).querySelector(s)};
var $$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
var lowPower=reduce||innerWidth<700||(navigator.hardwareConcurrency&&navigator.hardwareConcurrency<=4)||(navigator.connection&&navigator.connection.saveData);
if(lowPower)document.documentElement.classList.add("no-motion");

/* seeded RNG so layouts are stable between loads */
function rng(seed){var s=seed|0;return function(){s|=0;s=s+0x6D2B79F5|0;var t=Math.imul(s^s>>>15,1|s);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}

/* ---- config-driven links ---- */
$$("[data-invite]").forEach(function(a){var u=a.dataset.invite==="prime"?C.PRIME_INVITE_URL:C.WELCOME_INVITE_URL;if(u){a.href=u;a.rel="noopener"}});
$$("[data-support]").forEach(function(a){if(C.SUPPORT_SERVER_URL){a.href=C.SUPPORT_SERVER_URL;a.rel="noopener"}});
$$("[data-topgg]").forEach(function(a){if(C.TOPGG_URL){a.href=C.TOPGG_URL;a.rel="noopener"}else{a.hidden=true}});

/* ---- mobile menu ---- */
var mb=$(".menu-btn"),nav=$("#nav");
if(mb&&nav){
  mb.addEventListener("click",function(){var o=nav.classList.toggle("open");mb.setAttribute("aria-expanded",o)});
  document.addEventListener("keydown",function(e){if(e.key==="Escape"&&nav.classList.contains("open")){nav.classList.remove("open");mb.setAttribute("aria-expanded","false");mb.focus()}});
}

/* ---- cracking white paint: build shards ---- */
var GRID={t:[0,28,52,77,100],m:[[0,48],[24,56],[47,44],[73,53],[100,46]],b:[0,30,55,80,100]};
function shardPolys(){
  var p=[],t=GRID.t,m=GRID.m,b=GRID.b,i;
  for(i=0;i<4;i++){
    p.push([[t[i],0],[t[i+1],0],m[i+1],m[i]]);
    p.push([m[i],m[i+1],[b[i+1],100],[b[i],100]]);
  }
  return p;
}
function buildCrack(el){
  if(reduce)return;
  var wrap=document.createElement("span");wrap.className="shards";wrap.setAttribute("aria-hidden","true");
  var polys=shardPolys(),r=rng(7+el.textContent.length);
  polys.forEach(function(poly,i){
    var s=document.createElement("i");
    s.style.clipPath="polygon("+poly.map(function(q){return q[0]+"% "+q[1]+"%"}).join(",")+")";
    var cx=poly.reduce(function(a,q){return a+q[0]},0)/4-50,up=i%2===0;
    s.style.setProperty("--tx",(cx*1.2+(r()-.5)*30).toFixed(0)+"px");
    s.style.setProperty("--ty",(up?-40-r()*60:90+r()*120).toFixed(0)+"px");
    s.style.setProperty("--r",((r()-.5)*50).toFixed(0)+"deg");
    s.style.setProperty("--d",String(Math.abs(Math.round(cx/12))+(up?0:1)));
    wrap.appendChild(s);
  });
  var seam=document.createElement("span");seam.className="seam";seam.setAttribute("aria-hidden","true");
  el.appendChild(wrap);el.appendChild(seam);
}
$$(".crack").forEach(buildCrack);

/* ---- circuit traces (draw on scroll) ---- */
var NS="http://www.w3.org/2000/svg";
function buildCircuit(el,idx){
  var r=rng(31+idx*17),svg=document.createElementNS(NS,"svg");
  svg.setAttribute("viewBox","0 0 1200 80");svg.setAttribute("preserveAspectRatio","none");svg.setAttribute("class","circuit");svg.setAttribute("aria-hidden","true");
  var n=9;
  for(var i=0;i<n;i++){
    var dir=i%2?-1:1,x=dir>0?r()*120:1200-r()*120,y=10+r()*60,d="M"+x.toFixed(0)+" "+y.toFixed(0),jogs=2+Math.floor(r()*3);
    for(var j=0;j<jogs;j++){x+=dir*(80+r()*160);d+=" H"+Math.max(0,Math.min(1200,x)).toFixed(0);if(j<jogs-1){y=Math.max(6,Math.min(74,y+(r()<.5?-1:1)*(8+r()*22)));d+=" V"+y.toFixed(0)}}
    var p=document.createElementNS(NS,"path");p.setAttribute("d",d);p.setAttribute("pathLength","1");p.setAttribute("stroke-width",(.7+r()*.9).toFixed(1));p.setAttribute("opacity",(.35+r()*.6).toFixed(2));p.style.setProperty("--i",i);
    svg.appendChild(p);
    var b=document.createElementNS(NS,"rect");b.setAttribute("x",(Math.max(0,Math.min(1200,x))-3).toFixed(0));b.setAttribute("y",(y-2).toFixed(0));b.setAttribute("width",r()<.4?9:4);b.setAttribute("height",4);svg.appendChild(b);
  }
  el.replaceWith(svg);return svg;
}
var circuits=$$("[data-circuit]").map(buildCircuit);

/* ---- reveal observer ---- */
var targets=$$(".reveal,.crack").concat(circuits);
if("IntersectionObserver" in window&&!reduce){
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target)}})},{threshold:.15,rootMargin:"0px 0px -6% 0px"});
  targets.forEach(function(t){io.observe(t)});
}else targets.forEach(function(t){t.classList.add("in")});

/* ---- hero HUD: binary ring, circuits, telemetry ---- */
var bin=$("#bin textPath");
if(bin){var r1=rng(0x51ed270b),bits="";for(var i=0;i<150;i++)bits+=(r1()<.5?"0":"1")+(i%7===6?" ":"");bin.textContent=bits}
var cg=$("#circuits");
if(cg){
  var r2=rng(0x2f1c);
  ["L","R"].forEach(function(side){
    for(var i=0;i<15;i++){
      var dir=side==="L"?-1:1,y0=95+r2()*210,x0=380+dir*(150+r2()*50),len=60+r2()*(side==="L"?150:130),x=x0,y=y0,d="M"+x+" "+y,jogs=1+Math.floor(r2()*3);
      for(var j=0;j<jogs;j++){var seg=len/jogs;x+=dir*seg*(.4+r2()*.6);d+=" H"+x.toFixed(0);if(j<jogs-1||r2()<.5){y+=(r2()<.5?-1:1)*(6+r2()*16);d+=" V"+y.toFixed(0)}}
      var p=document.createElementNS(NS,"path");p.setAttribute("d",d);p.setAttribute("stroke-width",(.7+r2()*.9).toFixed(1));p.setAttribute("opacity",(.35+r2()*.6).toFixed(2));if(r2()<.3)p.setAttribute("stroke-dasharray","6 3 1 3");cg.appendChild(p);
      var b=document.createElementNS(NS,"rect");b.setAttribute("x",(x-2).toFixed(0));b.setAttribute("y",(y-2).toFixed(0));b.setAttribute("width",r2()<.4?7:3);b.setAttribute("height",3);b.setAttribute("fill","#e6f4ff");b.setAttribute("stroke","none");cg.appendChild(b);
    }
  });
}
var gl=$("#line");
if(gl){
  var N=64,vals=[],lag=[],i2;for(i2=0;i2<N;i2++){vals.push(.5);lag.push(.5)}
  var pts=function(a){return a.map(function(v,i){return (i/(N-1)*400).toFixed(1)+","+(70-v*62-4).toFixed(1)}).join(" ")};
  var draw=function(){
    vals.shift();var prev=vals[vals.length-1];vals.push(Math.max(.05,Math.min(.95,prev+(Math.random()-.5)*.55+(.5-prev)*.12)));
    lag.shift();lag.push(vals[vals.length-3]);
    gl.setAttribute("points",pts(vals));$("#lagline").setAttribute("points",pts(lag));$("#area").setAttribute("points","0,70 "+pts(vals)+" 400,70");
  };
  draw();
  if(!reduce){var tt=setInterval(draw,110);document.addEventListener("visibilitychange",function(){if(document.hidden){clearInterval(tt);tt=null}else if(!tt)tt=setInterval(draw,110)})}
}

/* ---- background node network canvas (single canvas, 30fps cap, pauses when hidden) ---- */
var cv=$("#bgfx");
if(cv&&!lowPower){
  var ctx=cv.getContext("2d"),W,H,dpr=Math.min(devicePixelRatio||1,1.5),nodes=[],pulses=[],R=rng(99);
  var resize=function(){W=cv.width=Math.floor(innerWidth*dpr);H=cv.height=Math.floor(innerHeight*dpr);var n=Math.min(70,Math.floor(innerWidth*innerHeight/26000));nodes=[];for(var i=0;i<n;i++)nodes.push({x:R()*W,y:R()*H,vx:(R()-.5)*.25*dpr,vy:(R()-.5)*.25*dpr})};
  resize();addEventListener("resize",resize);
  var last=0,raf=null,LINK=170*dpr;
  var frame=function(t){
    raf=requestAnimationFrame(frame);
    if(t-last<33)return;last=t;
    ctx.clearRect(0,0,W,H);
    var i,j,a,b,dx,dy,d;
    ctx.lineWidth=1;
    for(i=0;i<nodes.length;i++){a=nodes[i];a.x+=a.vx;a.y+=a.vy;if(a.x<0||a.x>W)a.vx*=-1;if(a.y<0||a.y>H)a.vy*=-1;
      for(j=i+1;j<nodes.length;j++){b=nodes[j];dx=a.x-b.x;dy=a.y-b.y;d=dx*dx+dy*dy;if(d<LINK*LINK){ctx.strokeStyle="rgba(190,225,255,"+(.22*(1-Math.sqrt(d)/LINK)).toFixed(3)+")";ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
        if(pulses.length<14&&Math.random()<.0009)pulses.push({a:a,b:b,p:0})}}
      ctx.fillStyle="rgba(225,243,255,.55)";ctx.fillRect(a.x-1,a.y-1,2*dpr,2*dpr)}
    for(i=pulses.length-1;i>=0;i--){var u=pulses[i];u.p+=.02;if(u.p>=1){pulses.splice(i,1);continue}
      var px=u.a.x+(u.b.x-u.a.x)*u.p,py=u.a.y+(u.b.y-u.a.y)*u.p;ctx.fillStyle="rgba(255,255,255,.95)";ctx.shadowColor="rgba(150,210,255,1)";ctx.shadowBlur=8*dpr;ctx.fillRect(px-2*dpr,py-2*dpr,4*dpr,4*dpr);ctx.shadowBlur=0}
  };
  raf=requestAnimationFrame(frame);
  document.addEventListener("visibilitychange",function(){if(document.hidden){cancelAnimationFrame(raf);raf=null}else if(!raf)raf=requestAnimationFrame(frame)});
}else if(cv){cv.hidden=true}

/* ---- cursor parallax (desktop pointers only) ---- */
if(!reduce&&matchMedia("(pointer:fine) and (hover:hover)").matches){
  var pars=$$(".par");
  if(pars.length){
    var mx=0,my=0,tx=0,ty=0,pr=null;
    addEventListener("pointermove",function(e){mx=(e.clientX/innerWidth-.5)*2;my=(e.clientY/innerHeight-.5)*2;if(!pr)pr=requestAnimationFrame(tick)},{passive:true});
    var tick=function(){tx+=(mx-tx)*.08;ty+=(my-ty)*.08;pars.forEach(function(el){var k=parseFloat(el.dataset.par||"10");el.style.transform="translate3d("+(tx*k).toFixed(2)+"px,"+(ty*k).toFixed(2)+"px,0)"});pr=(Math.abs(mx-tx)>.001||Math.abs(my-ty)>.001)?requestAnimationFrame(tick):null};
  }
}

/* ---- pricing toggle ---- */
var seg=$(".seg[data-plans]");
if(seg){
  var btns=$$("button",seg),plans=$$(".plan");
  var pick=function(k){btns.forEach(function(b){b.setAttribute("aria-pressed",b.dataset.plan===k)});plans.forEach(function(p){p.classList.toggle("on",p.dataset.plan===k)})};
  btns.forEach(function(b){b.addEventListener("click",function(){pick(b.dataset.plan)})});
  pick("yearly");
}

/* ---- commands search + filter ---- */
var cin=$("#cmd-q");
if(cin){
  var rows=$$(".cmd-row"),cats=$$(".cmd-cat"),chips=$$(".chip"),cur="all",cnt=$("#cmd-count"),emp=$("#cmd-empty");
  var apply=function(){
    var q=cin.value.trim().toLowerCase().replace(/^\//,""),shown=0;
    rows.forEach(function(r){var ok=(cur==="all"||r.dataset.cat===cur)&&(!q||r.dataset.s.indexOf(q)>-1);r.hidden=!ok;if(ok)shown++});
    cats.forEach(function(c){c.hidden=!$$(".cmd-row:not([hidden])",c).length});
    cnt.textContent=shown+" shown";emp.hidden=shown>0;
  };
  cin.addEventListener("input",apply);
  chips.forEach(function(c){c.addEventListener("click",function(){cur=c.dataset.cat;chips.forEach(function(x){x.setAttribute("aria-pressed",x===c)});apply()})});
  apply();
}

/* ---- live-ready stats strip: hides itself unless the endpoint answers ---- */
var st=$("#stats");
if(st&&C.STATS_API_URL&&window.fetch){
  var ctl=window.AbortController?new AbortController():null,to=setTimeout(function(){ctl&&ctl.abort()},4000);
  fetch(C.STATS_API_URL,{headers:{Accept:"application/json"},signal:ctl&&ctl.signal}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(d){
    var map=[["servers","Servers"],["commands","Commands"]],h="";
    map.forEach(function(m){if(typeof d[m[0]]==="number")h+='<div class="stat"><b>'+d[m[0]].toLocaleString("en-US")+"</b><span>"+m[1]+"</span></div>"});
    if(h){st.innerHTML=h;st.classList.add("on")}
  }).catch(function(){}).then(function(){clearTimeout(to)});
}
})();
