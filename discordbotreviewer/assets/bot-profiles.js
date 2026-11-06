(()=> {
const bots={
 "mee6":{id:"159985870458322944",servers:"20.9M",revenue:"Multi-million annual",value:"Not disclosed",evidence:"Company revenue described as multi-million in a public interview; exact MRR and valuation not disclosed.",confidence:"Revenue evidence: public company statement"},
 "dyno":{id:"155149108183695360",servers:"11.7M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Paid plans are public; paid-server count and revenue are not.",confidence:"Low financial visibility"},
 "carl-bot":{id:"235148962103951360",servers:"15.3M",revenue:"Approx. $3K–$18K/mo Patreon estimate",value:"Not reliably estimable",evidence:"Third-party Patreon estimates vary widely and exclude direct Discord subscriptions.",confidence:"Low to medium; Patreon is incomplete"},
 "yagpdb":{id:"204255221017214977",servers:"3.5M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"No verified current revenue disclosure located.",confidence:"Low financial visibility"},
 "probot":{id:"282859044593598464",servers:"10.3M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Public paid plans exist; paid conversion and revenue are not disclosed.",confidence:"Low financial visibility"},
 "arcane":{id:"437808476106784770",servers:"3.1M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Public paid plans exist; paid conversion and revenue are not disclosed.",confidence:"Low financial visibility"},
 "tatsu":{id:"172002275412279296",servers:"1.48M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Monetization exists, but current financial results are not public.",confidence:"Low financial visibility"},
 "dank-memer":{id:"270904126974590976",servers:"8.6M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Paid features exist; no verified current revenue disclosure located.",confidence:"Low financial visibility"},
 "mudae":{id:"432610292342587392",servers:"3.37M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Paid features exist; current paid conversion and revenue are not public.",confidence:"Low financial visibility"},
 "poketwo":{id:"716390085896962058",servers:"1.48M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Public listing describes the bot as free and open source; no verified revenue disclosed.",confidence:"Low financial visibility"},
 "fredboat":{id:"184405311681986560",servers:"7.6M historical directory estimate",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"No verified current revenue disclosure located.",confidence:"Low financial visibility"},
 "jockie-music":{id:"411916947773587456",servers:"9.8M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Premium exists; revenue and paid-user counts are not public.",confidence:"Low financial visibility"},
 "xenon":{id:"416358583220109598",servers:"3.3M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Premium pricing is public; customer count and revenue are not.",confidence:"Low financial visibility"}
};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
document.querySelectorAll(".archive-card").forEach(card=>{
 const slug=(new URL(card.href,location.href).searchParams.get("article")||"").toLowerCase();
 const name=card.querySelector("h3")?.textContent?.trim()||"Bot";
 const data=bots[slug]||{revenue:"Not publicly disclosed",value:"Not reliably estimable",evidence:"No verified financial disclosure found. Popularity or server count alone cannot establish revenue or company value.",confidence:"Not enough public financial data"};
 const img=document.createElement("img");img.className="archive-bot-avatar";img.alt=name+" official Discord bot avatar";img.loading="lazy";img.decoding="async";
 if(data.id){
   img.src="https://cdn.discordapp.com/embed/avatars/0.png";
   card.prepend(img);
   fetch("https://japi.rest/discord/v1/application/"+data.id).then(r=>{if(!r.ok)throw new Error("avatar lookup failed");return r.json()}).then(payload=>{
     const profile=payload.data||{};
     const url=profile.application?.iconURL||profile.bot?.avatarURL||null;
     if(url) img.src=url;
     else img.classList.add("archive-avatar-fallback");
   }).catch(()=>img.classList.add("archive-avatar-fallback"));
 } else {
   img.src="https://cdn.discordapp.com/embed/avatars/0.png";
   img.classList.add("archive-avatar-fallback");
   card.prepend(img);
 }
 const financial=document.createElement("div");financial.className="bot-financials";
 financial.innerHTML='<div><span>Server reach</span><strong>'+esc(data.servers||"Not verified")+'</strong></div><div><span>Monthly revenue</span><strong>'+esc(data.revenue)+'</strong></div><div><span>Estimated value</span><strong>'+esc(data.value)+'</strong></div><div class="bot-estimate-note">'+esc(data.confidence)+'. '+esc(data.evidence)+'</div>';card.append(financial);
});
})();