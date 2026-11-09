(()=>{
 const root=document.getElementById("article-slideshow");if(!root)return;
 const slides=[...root.querySelectorAll(".slideshow-slide")],dots=[...root.querySelectorAll(".slideshow-dots button")],counter=root.querySelector(".slideshow-counter");
 let active=0,timer=null;
 function show(index){active=(index+slides.length)%slides.length;slides.forEach((s,i)=>{s.classList.toggle("is-active",i===active);s.setAttribute("aria-hidden",String(i!==active))});dots.forEach((d,i)=>{d.classList.toggle("is-active",i===active);d.setAttribute("aria-current",i===active?"true":"false")});if(counter)counter.textContent=String(active+1).padStart(2,"0")+" / "+String(slides.length).padStart(2,"0")}
 function restart(){if(timer)clearInterval(timer);if(!window.matchMedia("(prefers-reduced-motion: reduce)").matches)timer=setInterval(()=>show(active+1),6500)}
 root.querySelector("[data-slide-prev]")?.addEventListener("click",()=>{show(active-1);restart()});root.querySelector("[data-slide-next]")?.addEventListener("click",()=>{show(active+1);restart()});dots.forEach((d,i)=>d.addEventListener("click",()=>{show(i);restart()}));
 root.addEventListener("mouseenter",()=>{if(timer)clearInterval(timer)});root.addEventListener("mouseleave",restart);root.addEventListener("focusin",()=>{if(timer)clearInterval(timer)});root.addEventListener("focusout",restart);
 slides.forEach(slide=>{const img=slide.querySelector("img");const id=slide.dataset.id;if(!img||!id||img.getAttribute("src"))return;fetch("https://japi.rest/discord/v1/application/"+encodeURIComponent(id)).then(r=>{if(!r.ok)throw new Error("avatar unavailable");return r.json()}).then(data=>{const url=data.data?.application?.iconURL||data.data?.bot?.avatarURL;if(url)img.src=url}).catch(()=>{img.remove()})});
 show(0);restart();
})();