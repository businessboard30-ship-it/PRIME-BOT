(()=>{
 const chips=[...document.querySelectorAll("[data-filter]")];
 const cards=[...document.querySelectorAll(".archive-card")];
 const empty=document.querySelector(".archive-empty");
 const search=document.getElementById("archive-search-input");
 let active="all",query="";
 function filter(){
  let visible=0;
  cards.forEach(card=>{
   const category=(card.dataset.category||"").toLowerCase();
   const haystack=(card.innerText+" "+(card.dataset.tags||"")).toLowerCase();
   const matchesCategory=active==="all"||category===active.toLowerCase();
   const matchesQuery=!query||haystack.includes(query);
   const show=matchesCategory&&matchesQuery;
   card.hidden=!show;
   if(show)visible++;
  });
  chips.forEach(chip=>chip.classList.toggle("is-active",chip.dataset.filter===active));
  if(empty)empty.hidden=visible!==0;
 }
 chips.forEach(chip=>chip.addEventListener("click",()=>{active=chip.dataset.filter;filter()}));
 if(search)search.addEventListener("input",()=>{query=search.value.trim().toLowerCase();filter()});
 cards.forEach(card=>{
  const category=card.dataset.category||"Other";
  const title=card.querySelector("h3")?.textContent||"";
  const desc=card.querySelector("p")?.textContent||"";
  const tags=[category,...(title+" "+desc).match(/AI|voice|music|moderation|security|economy|games|leveling|welcome|tickets|verification|analytics|roles|community|events|automation|utility|feeds|polls|backups|discovery/gi)||[]];
  card.dataset.tags=[...new Set(tags)].join(" ");
  const tagRow=document.createElement("div");tagRow.className="archive-card-tags";
  [...new Set(tags)].slice(0,4).forEach(tag=>{const el=document.createElement("span");el.className="archive-tag";el.textContent=tag;tagRow.append(el)});
  const description=card.querySelector("p");if(description)description.after(tagRow);
 });
 filter();
})();