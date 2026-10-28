import { motion, onMotionChange, announce } from './common.js?v=m14-1';
import { createHeroField, createLabFlow, decryptText } from './effects.js?v=public-records-5';
import { mountResearchView } from './research-view.js?v=m14-1';
import { createServiceClient } from './service-client.js?v=m11-1';
import { createResearchAdapter } from './research-adapter.js?v=m11-1';
import { mountContribution } from './contribution.js?v=m14-1';

const $=id=>document.getElementById(id);
const staticView=new URLSearchParams(location.search).get('render')==='static';
const inert={setPaused(){},setPhase(){},setMode(){},setOutcome(){},dispose(){}};
const hero=staticView?inert:createHeroField($('hero-field'),$('hero'),null);
const lab=staticView?inert:createLabFlow($('lab-field'));
let researchView=null;
const service=createServiceClient(), contribution=mountContribution(service);
service.connect().then(async()=>{
  await contribution.connected();
  researchView=await mountResearchView(lab,createResearchAdapter(service));
  researchView.onMotionChange(motion);
}).catch(()=>announce('The research interface could not be connected.'));
onMotionChange(state=>{hero.setPaused(state.paused);researchView?.onMotionChange(state);});
document.querySelectorAll('[data-decrypt]').forEach(element=>decryptText(element,element.dataset.decrypt,motion.reduced));
let scrollFrame=0;
function updateMemory(){
  scrollFrame=0;
  const timeline=document.querySelector('.memory-timeline'),rect=timeline.getBoundingClientRect();
  const progress=Math.min(1,Math.max(0,(innerHeight*.64-rect.top)/rect.height));
  timeline.style.setProperty('--memory-progress',Math.round(progress*100)+'%');
}
window.addEventListener('scroll',()=>{if(!scrollFrame)scrollFrame=requestAnimationFrame(updateMemory);},{passive:true});
window.addEventListener('resize',updateMemory);updateMemory();
fetch('./data/report.json').then(response=>{
  if(!response.ok)throw Error('Record unavailable');return response.json();
}).then(report=>{
  if(!Number.isInteger(report.passed_count)||!Number.isInteger(report.case_count))throw Error('Unexpected report');
  $('probe-count').textContent=report.passed_count+' / '+report.case_count;
}).catch(()=>{$('probe-count').textContent='Unavailable';});
window.addEventListener('pagehide',event=>{
  if(!event.persisted){researchView?.dispose();hero.dispose();lab.dispose();cancelAnimationFrame(scrollFrame);}
});
