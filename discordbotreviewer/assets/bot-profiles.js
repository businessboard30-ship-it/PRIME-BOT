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
 "fredboat":{id:"184405253028970496",servers:"7.6M historical directory estimate",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"No verified current revenue disclosure located.",confidence:"Low financial visibility"},
 "jockie-music":{id:"411916947773587456",servers:"9.8M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Premium exists; revenue and paid-user counts are not public.",confidence:"Low financial visibility"},
 "xenon":{id:"416358583220109598",servers:"3.3M",revenue:"Not disclosed",value:"Not reliably estimable",evidence:"Premium pricing is public; customer count and revenue are not.",confidence:"Low financial visibility"}
,"hydra":{id:"547905866255433758"},"wick":{id:"536991182035746816"},"rythm":{id:"235088799074484224"},"statbot":{id:"491769129318088714"},"serverstats":{id:"458276816071950337"},"mimu":{id:"493716749342998541"},"captcha-bot":{id:"512333785338216465"},"double-counter":{id:"703886990948565003"},"apollo":{id:"475744554910351370"},
 "unbelievaboat":{id:"292953664492929025"},
 "helper-gg":{id:"641385620530266125"},
 "ticket-tool":{id:"557628352828014614"},
 "altdentifier":{id:"372022813839851520"},
 "sapphire":{id:"678344927997853742"},
 "streamcord":{id:"375805687529209857"},
 "owobot":{id:"408785106942164992"},
 "karuta":{id:"646937666251915264"},
 "epic-rpg":{id:"555955826880413696"},
 "maki":{id:"563434444321587202"},
 "zira":{id:"275813801792634880"},
 "sesh":{id:"616754792965865495"},
 "disboard":{id:"302050872383242240"},
 "tempvoice":{id:"762217899355013120"},
 "birthdaybot":{id:"656621136808902656"},
 "amaribot":{id:"339254240012664832"},
 "groovy":{id:"234395307759108106"},
 "miyu":{id:"1376962306726039572"},
 "betty":{id:"884800931819048980"},
 "pixelmint":{id:"1500754402741588019"}
};
window.DBR_BOT_PROFILES=bots;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
document.querySelectorAll(".archive-card").forEach(card=>{
 const slug=(new URL(card.href,location.href).searchParams.get("article")||"").toLowerCase();
 const name=card.querySelector("h3")?.textContent?.trim()||"Bot";
 const data={revenue:"Not publicly disclosed",value:"Not reliably estimable",evidence:"No verified financial disclosure found. Popularity or server count alone cannot establish revenue or company value.",confidence:"Not enough public financial data",...(bots[slug]||{})};
 const img=document.createElement("img");img.className="archive-bot-avatar";img.alt=name+" official Discord bot avatar";img.loading="lazy";img.decoding="async";
 if(data.id){
   fetch("https://japi.rest/discord/v1/application/"+data.id).then(r=>{if(!r.ok)throw new Error("avatar lookup failed");return r.json()}).then(payload=>{
     const profile=payload.data||{};
     const url=profile.application?.iconURL||profile.bot?.avatarURL||null;
     if(!url)throw new Error("No custom Discord avatar is published");
     img.src=url;img.onload=()=>img.classList.add("is-loaded");card.prepend(img);
   }).catch(()=>img.remove());
 }
 const financial=document.createElement("div");financial.className="bot-financials";
 financial.innerHTML='<div><span>Server reach</span><strong>'+esc(data.servers||"Not verified")+'</strong></div><div><span>Monthly revenue</span><strong>'+esc(data.revenue)+'</strong></div><div><span>Estimated value</span><strong>'+esc(data.value)+'</strong></div><div class="bot-estimate-note">'+esc(data.confidence)+'. '+esc(data.evidence)+'</div>';card.append(financial);
});
})();