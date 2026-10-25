import { announce } from './common.js?v=public-records-5';
const articles=Array.from(document.querySelectorAll('[data-doc]'));
const links=Array.from(document.querySelectorAll('[data-doc-link]'));
const sidebar=document.querySelector('.docs-sidebar');
const toggle=document.getElementById('docs-menu-toggle');
const search=document.getElementById('docs-search');
let current='mission';
articles.forEach(article=>{
  const id=article.dataset.doc;
  article.querySelectorAll('.doc-copy h2').forEach((heading,i)=>{heading.id=id+'--section-'+i;heading.setAttribute('tabindex','-1');});
});
function showTopic(shouldFocus=false) {
  const hash=decodeURIComponent(location.hash.slice(1));
  const requested=hash.split('--')[0]||'mission';
  const selected=articles.find(article=>article.dataset.doc===requested)||articles[0];
  current=selected.dataset.doc;
  articles.forEach(article=>article.hidden=article!==selected);
  links.forEach(link=>{
    const active=link.dataset.docLink===current;
    link.classList.toggle('active',active);
    if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
  document.title='EVORPH Docs — '+selected.querySelector('h1').textContent;
  document.getElementById('docs-onpage-nav').replaceChildren();
  selected.querySelectorAll('.doc-copy h2').forEach(heading=>{
    const anchor=document.createElement('a');anchor.href='#'+heading.id;anchor.textContent=heading.textContent;
    document.getElementById('docs-onpage-nav').append(anchor);
  });
  sidebar.classList.remove('menu-open');toggle.setAttribute('aria-expanded','false');
  if(shouldFocus) {
    const target=hash.includes('--')?document.getElementById(hash):selected.querySelector('h1');
    if(target){target.focus({preventScroll:true});target.scrollIntoView({behavior:'auto',block:'start'});}
  }
}
window.addEventListener('hashchange',()=>showTopic(true));
toggle.addEventListener('click',()=>{
  const open=!sidebar.classList.contains('menu-open');
  sidebar.classList.toggle('menu-open',open);toggle.setAttribute('aria-expanded',String(open));
  if(open)search.focus();
});
search.addEventListener('input',()=>{
  const query=search.value.trim().toLowerCase();let count=0;
  links.forEach(link=>{
    const article=articles.find(item=>item.dataset.doc===link.dataset.docLink);
    const matches=!query||(article.textContent+' '+link.textContent).toLowerCase().includes(query);
    link.hidden=!matches;if(matches)count++;
  });
  document.querySelectorAll('.docs-nav-group').forEach(group=>group.hidden=!!query);
  document.getElementById('docs-no-results').hidden=count>0;
});
document.querySelectorAll('[data-copy-doc]').forEach(button=>button.addEventListener('click',async()=>{
  const url=new URL('docs.html#'+button.dataset.copyDoc,location.href).href;
  try{await navigator.clipboard.writeText(url);announce('Documentation link copied.');}
  catch{announce('The page URL is ready in the address bar. Copy it from there.');}
}));
showTopic();
