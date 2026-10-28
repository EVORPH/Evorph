import { classifyExperiment, orderedRecords, safeEvidenceHref, SOURCE_ORIGIN } from './model.mjs';
import { formatUTC } from './time.mjs';
import { createArchiveReader } from './data.mjs';

const $ = (selector, base = document) => base.querySelector(selector);
const $$ = (selector, base = document) => [...base.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const pad = n => String(n).padStart(2,'0');
const status = (value, label = value) => `<span class="status status-${esc(value)}">${esc(label)}</span>`;
const labels = {seeker:'SEEKER',keeper:'KEEPER',verifier:'Fixed program',system:'Archive system',contributor:'Contributor'};
const assertions = {unverified:'Unverified statement',supported:'Supported by program evidence',refuted:'Refuted by program evidence',inconclusive:'Inconclusive',not_applicable:'Not an experimental claim'};
const phases = {observe:'Observe',explore:'Explore',find:'Find',repair:'Repair',verify:'Verify',retain:'Retain'};
const routeNames = {lab:'Observation room',success:'Successful',failures:'Unsuccessful',provenance:'Sources & method'};
let archive = null, bundles = [], current = null, records = [], position = 0, timer = null, delay = 3200, motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
let filter = 'all', search = '', renderedLab = null;
const app = $('#app');
const byRecord = new Map();
const byVersion = new Map();
const reader = createArchiveReader();
const viewParam = () => new URLSearchParams(location.search).get('view');
const route = () => ['success', 'failures', 'provenance'].includes(viewParam()) ? viewParam() : 'lab';
const viewURL = (view, params = {}) => {
  const query = new URLSearchParams({ view });
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  return location.pathname + '?' + query.toString();
};
const archiveURL = bundle => viewURL('lab', { experiment: bundle.experiment.experiment_id });
const recordURL = (bundle, record) => viewURL('lab', { experiment: bundle.experiment.experiment_id, record: record.record_id });
const sourceURL = bundle => `${SOURCE_ORIGIN}/api/research/experiments/${encodeURIComponent(bundle.experiment.experiment_id)}/records?limit=100`;
const capture = () => archive && archive.read_at ? formatUTC(archive.read_at) : 'Reading…';
const coverage = () => !archive ? 'Waiting for the archive'
  : archive.error && !bundles.length ? 'Read failed'
  : `${bundles.length} of ${archive.experiments.length} experiments classified${archive.complete ? '' : ' so far'}`;
const captureHTML = () => `<div class="capture-date">READ FROM THE PUBLIC ARCHIVE<strong>${esc(capture())}</strong><small>${esc(coverage())}</small></div>`;
const partialNotice = () => {
  if (!archive) return '';
  const notes = [];
  if (!archive.complete && !archive.error) notes.push(`Still reading: ${bundles.length} of ${archive.experiments.length} experiments classified. Counts and lists grow as each experiment is read.`);
  if (archive.error) notes.push(`The archive read stopped early: ${archive.error}`);
  if (archive.failures.length) notes.push(`${archive.failures.length} record read(s) failed and are excluded from these counts: ${archive.failures.map(item => item.experiment_id).join(', ')}.`);
  return notes.length ? `<div class="archive-notice">${icon('source')}<p><strong>Partial history.</strong> ${esc(notes.join(' '))}</p></div>` : '';
};
function heading(title, description) { return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${captureHTML()}</div>`; }
function announce(text) { $('#announcement').textContent = text; }
function stop() { if (timer) clearInterval(timer); timer = null; const description=$('#replay-description'); if(description) description.textContent='Saved records · paused'; const play = $('#play'); if(play){ play.innerHTML = icon('play'); play.setAttribute('aria-label','Play historical records'); play.setAttribute('aria-pressed','false'); } $$('.role-station').forEach(node=>node.dataset.playing='false'); }
function navigate(url, replace = false) { stop(); history[replace?'replaceState':'pushState']({},'',url); renderRoute(); window.scrollTo({top:0,behavior:'instant'}); $('#main').focus({preventScroll:true}); }
function setLocation(record) { const url = new URL(location.href); url.searchParams.set('experiment',current.experiment.experiment_id); if(record) url.searchParams.set('record',record.record_id); else url.searchParams.delete('record'); history.replaceState({},'',url); }
function reference(id, caption) {
  const found = byRecord.get(id);
  return found ? `<button class="record-ref" data-record="${esc(id)}">${esc(caption || `#${found.record.sequence} · ${found.record.title}`)} ${icon('arrow')}</button>` : `<span class="record-ref missing">${esc(caption || id)} · not in the records read so far</span>`;
}
function versionReference(id) {
  if (!id) return '<span>None · initial version</span>';
  const found = byVersion.get(id);
  return found ? `<button class="record-ref" data-record="${esc(found.record.record_id)}">${esc(id)} ${icon('arrow')}</button>` : `<span>${esc(id)} · no origin record read yet</span>`;
}
function metadata(values) { return `<dl class="meta-grid">${values.map(([name,value])=>`<div><dt>${esc(name)}</dt><dd>${esc(value ?? 'Not supplied')}</dd></div>`).join('')}</dl>`; }
function comparisonHTML(record) {
  const cmp = record.comparison;
  if(!cmp) return '';
  return `<section class="detail-section"><h3>Before & after ${status(cmp.conclusion)}</h3>${cmp.metrics.map(metric=>`<div class="compare-metric"><strong>${esc(metric.baseline)}</strong>${icon('arrow')}<strong>${esc(metric.candidate)}</strong><span>${esc(metric.label)}<br>${esc(metric.unit)} · ${esc(metric.direction === 'neutral'?'no preferred direction':metric.direction+' is better')}</span></div>`).join('')}<p>${esc(cmp.explanation)}</p>${metadata([['BASELINE VERSION',cmp.baseline_version_id],['CANDIDATE VERSION',cmp.candidate_version_id],['SPECIFICATION',cmp.spec_version],['TEST SET',cmp.test_set_version],['MODEL CONFIGURATION',cmp.model_config_id],['RESOURCE PROFILE',cmp.resource_conditions.profile_id],['COMPARABLE CONDITIONS',cmp.resource_conditions.matched?'Matched · reported by service':'Unmatched · incomparable']])}<p class="feedback-note">These are service-reported conditions and results. This interface does not independently certify them.</p></section>`;
}
function recordDetails(record) {
  const evidence = record.evidence || [], verification = record.verification;
  return `<div class="inspector-top"><div><span>${esc(labels[record.speaker] || record.speaker)}</span><span class="tiny">/ ${esc(phases[record.phase] || record.phase)}</span></div><span class="tiny">SEQ ${pad(record.sequence)}</span></div>
  <h2>${esc(record.title)}</h2>
  <div>${status(record.assertion,assertions[record.assertion] || record.assertion)}</div>
  ${record.public_text?'<p class="inspector-text detail-section" id="public-text"></p>':''}
  ${record.summary && record.summary !== record.public_text?`<p class="inspector-summary">${esc(record.summary)}</p>`:''}
  ${record.speaker==='seeker'||record.speaker==='keeper'?`<p class="assertion-note">${icon('source')}Intentionally public AI note. It is a proposal or observation, not private internal reasoning or a verified result.</p>`:''}
  ${verification?`<section class="detail-section"><h3>Fixed-program verification ${status(verification.outcome)}</h3><p>${esc(verification.summary)}</p>${metadata([['TEST IDS',verification.test_ids.join('\n') || 'None supplied'],['EVIDENCE REFERENCES',verification.evidence_ids.join('\n') || 'None supplied']])}</section>`:''}
  ${comparisonHTML(record)}
  ${record.decision?`<section class="detail-section"><h3>Version decision ${status(record.decision.outcome)}</h3><p>${esc(record.decision.reason)}</p><div class="detail-label">DECISION BASED ON</div><div class="relationship-links">${record.decision.supporting_record_ids.length?record.decision.supporting_record_ids.map(id=>reference(id)).join(''):'No supporting record IDs supplied.'}</div></section>`:''}
  ${record.reply_to?.length?`<section class="detail-section"><h3>Responding to</h3><div class="relationship-links">${record.reply_to.map(id=>reference(id)).join('')}</div></section>`:''}
  ${record.candidate?`<section class="detail-section"><h3>Version lineage</h3><div class="detail-label">CANDIDATE</div><div class="relationship-links">${versionReference(record.candidate.version_id)}</div><div class="detail-label detail-section">PARENT VERSION</div><div class="relationship-links">${versionReference(record.candidate.parent_version_id)}</div></section>`:''}
  <section class="detail-section"><h3>Evidence ${evidence.length?`<span class="tiny">${evidence.length} reference${evidence.length===1?'':'s'}</span>`:''}</h3>${evidence.length?`<ul class="evidence-list">${evidence.map(item=>{const href=safeEvidenceHref(item.href);return `<li>${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(item.label)} ${icon('external')}</a>`:`<span>${esc(item.label)} · ${item.href?'Link unavailable':'Download not published'}</span>`}<small>${esc(item.evidence_id)}</small></li>`}).join('')}</ul>`:'<p class="empty-inline">No evidence attachment on this record. Follow its related records for supporting checks.</p>'}</section>
  ${record.contributions?.length?`<section class="detail-section"><h3>Contribution trail</h3>${record.contributions.map(c=>`<p>${esc(c.kind)} · ${esc(c.contributor_id)}</p>${metadata([['CONTRIBUTION',c.contribution_id],['SUBMISSION',c.submission_id]])}<div class="relationship-links">${reference(c.origin_record_id,'Original contribution')}</div>`).join('')}</section>`:''}
  <section class="detail-section"><h3>Record provenance</h3>${metadata([['RECORD ID',record.record_id],['SOURCE',record.source?.origin],['SOURCE ID',record.source?.source_id],['MODEL CONFIGURATION',record.source?.model_config_id],['EXPERIMENT',record.experiment_id],['RECORD TYPE',record.type]])}</section>
  <details class="raw-record"><summary>Inspect the complete source record</summary><pre id="raw-record"></pre></details>`;
}
function machine(role) {
  const seeker = role === 'seeker';
  return `<section class="role-station ${role}" id="station-${role}" data-active="false" data-playing="false" aria-label="${role.toUpperCase()} public notes"><div class="role-meta"><strong class="role-name">${role.toUpperCase()}</strong><span class="role-purpose">${seeker?'Find the blind spot':'Make the repair hold'}</span></div><button class="computer" data-role-note="${role}" aria-label="Read ${role.toUpperCase()} public note at this point"><div class="computer-top" aria-hidden="true"><div class="computer-screen"><div class="screen-code"><span>${seeker?'SKR':'KPR'} / ${seeker?'01':'02'}</span><span>ARCHIVE</span></div><div class="screen-face"><i class="eye"></i><i class="eye"></i></div><span class="screen-label">PUBLIC NOTE</span></div><span class="computer-brand"></span><span class="power-dot"></span><span class="disk-slot"></span></div><div class="computer-base" aria-hidden="true"></div></button><p class="role-note" id="note-${role}"></p><button class="read-role-note" data-role-note="${role}">Read public note ${icon('arrow')}</button></section>`;
}
function renderLab() {
  const query = new URLSearchParams(location.search);
  const requestedId = query.get('experiment');
  current = bundles.find(b=>b.experiment.experiment_id===requestedId) || null;
  if (!current && requestedId) {
    renderedLab = null;
    const known = Boolean(archive && archive.experiments.some(item=>item.experiment_id===requestedId));
    app.innerHTML = heading(known?'Reading this experiment.':'Unknown experiment.', known?'Its public records are being read from the archive now.':'The archive did not list an experiment with this identifier.') + partialNotice() + `<a class="text-link" data-route href="${viewURL('lab')}">Back to the first classified experiment ${icon('arrow')}</a>`;
    return;
  }
  if (!current) current = bundles.find(b=>b.result.category==='success') || bundles[0] || null;
  if (!current) { renderedLab = null; app.innerHTML = heading('The archive is loading.','No experiment has been classified yet.') + partialNotice() + `<a class="text-link" data-route href="${viewURL('provenance')}">How this page reads the archive ${icon('arrow')}</a>`; return; }
  renderedLab = current.experiment.experiment_id;
  records = orderedRecords(current);
  const requested = records.findIndex(r=>r.record_id===query.get('record'));
  position = requested>=0?requested:Math.max(0,records.findIndex(r=>r.speaker==='keeper'));
  const result = current.result;
  app.innerHTML = heading('Observation room.','Follow two research roles from a public proposal to the checks and decisions it leads to.') + `
  <div class="experiment-bar"><div class="experiment-select-wrap"><label for="experiment-select">EXPERIMENT</label><select id="experiment-select">${bundles.map(b=>`<option value="${esc(b.experiment.experiment_id)}"${b===current?' selected':''}>${pad(b.number)} / ${esc(b.result.label)} · ${b.records.length} records</option>`).join('')}</select></div><div class="experiment-status-line">${status(result.category,result.label)}<span class="run-state">Run: ${esc(current.experiment.state)}</span></div></div>
  <div class="lab-stage"><div class="stage-header"><span>${icon('archive')} THE RETIRED KEY · SAVED PUBLIC EXCHANGE</span><button id="motion-toggle" aria-pressed="${!motion}">${icon(motion?'pause':'play')}<span>${motion?'Pause motion':'Enable motion'}</span></button></div><div class="machine-row">${machine('seeker')}<div class="stage-bridge"><div class="bridge-glyph">${icon('source')}</div><strong>FIXED<br>VERIFICATION</strong><span>Program, not an AI</span></div>${machine('keeper')}</div><div class="stage-bottom"><span id="stage-event">No record selected</span><span>VISUAL CHARACTERS · RECORDED NOTES</span></div></div>
  <div class="replay-toolbar" role="group" aria-label="Historical replay controls"><button id="previous" class="icon-button" aria-label="Previous record">${icon('prev')}</button><button id="play" class="icon-button play-button" aria-label="Play historical records" aria-pressed="false">${icon('play')}</button><button id="next" class="icon-button" aria-label="Next record">${icon('next')}</button><div class="timeline-scrub"><label class="sr-only" for="scrub">Select recorded event</label><input type="range" id="scrub" min="0" max="${Math.max(0,records.length-1)}" value="${position}" step="1"><span class="record-counter" id="record-counter"></span></div><span class="replay-description" id="replay-description">Saved records · manual replay</span><label class="sr-only" for="speed">Replay speed</label><select id="speed" class="speed-select"><option value="5000">Slow</option><option value="3200" selected>Normal</option><option value="1500">Fast</option></select></div>
  <div class="trace-workspace"><section class="record-list-section"><div class="panel-title"><h2>Experiment trail</h2><span>${records.length} public events</span></div><ol class="record-list" id="record-list" aria-label="Ordered public research records">${records.map((r,i)=>`<li><button data-position="${i}" aria-current="false"><span class="record-number">${pad(r.sequence)}</span><span class="record-list-text"><small>${esc(labels[r.speaker])} / ${esc(phases[r.phase])}</small><strong>${esc(r.title)}</strong>${r.verification?`<em>${esc(r.verification.outcome)}</em>`:r.comparison?`<em>${esc(r.comparison.conclusion)}</em>`:r.decision?`<em>${esc(r.decision.outcome)}</em>`:''}</span></button></li>`).join('')}</ol></section><article id="inspector" class="inspector" tabindex="-1" aria-label="Selected record detail"></article></div>
  <div class="outcome-strip"><div><strong>Archived outcome · ${esc(result.label)}</strong><p>${esc(result.reason)}</p></div><a class="text-link" data-route href="${viewURL(result.category==='success'?'success':'failures')}">${result.category==='success'?'See what was retained':'Open the outcome archive'} ${icon('arrow')}</a></div><p class="feedback-note">Only intentionally public notes are shown. Sequence is factual; replay speed does not represent elapsed experiment time. A completed run is not automatically a successful repair.</p>`;
  $('#experiment-select').addEventListener('change',event=>navigate(archiveURL(bundles.find(b=>b.experiment.experiment_id===event.target.value))));
  $('#scrub').addEventListener('input',event=>{stop();selectRecord(Number(event.target.value));});
  $('#speed').value=String(delay);
  $('#speed').addEventListener('change',event=>{delay=Number(event.target.value);if(timer){stop();start();}});
  selectRecord(position,true);
  $('#replay-description').textContent='Saved records · manual replay';
  setMotion();
}
function setMotion() { $$('.role-station').forEach(node=>node.dataset.playing=String(Boolean(timer)&&motion)); }
function selectRecord(index, scroll = true) {
  if(!records.length){ $('#inspector').innerHTML='<p class="empty-inline">No public records were captured for this experiment.</p>'; ['previous','next','play','scrub','speed','motion-toggle'].forEach(id=>$('#'+id).disabled=true); $$('[data-role-note]').forEach(button=>button.disabled=true); ['seeker','keeper'].forEach(role=>$('#note-'+role).textContent='No public note captured.'); $('#record-counter').textContent='00 / 00'; $('#stage-event').textContent='No recorded events'; return; }
  position=Math.max(0,Math.min(index,records.length-1));
  const record=records[position];
  $('#scrub').value=String(position); $('#scrub').setAttribute('aria-valuetext',`Record ${position+1} of ${records.length}, sequence ${record.sequence}, ${record.title}`);
  $('#previous').disabled=position===0; $('#next').disabled=position===records.length-1; $('#play').disabled=records.length<2; $('#scrub').disabled=records.length<2; $('#speed').disabled=records.length<2;
  $('#record-counter').textContent=`${pad(position+1)} / ${pad(records.length)}`;
  $('#stage-event').textContent=`SEQ ${pad(record.sequence)} / ${labels[record.speaker]} · ${phases[record.phase]}`;
  $$('#record-list button').forEach((node,i)=>node.setAttribute('aria-current',String(i===position)));
  if(scroll) centerSelectedRecord();
  ['seeker','keeper'].forEach(role=>{
    const latest=records.slice(0,position+1).filter(r=>r.speaker===role).at(-1);
    $('#note-'+role).textContent=latest?latest.public_text || latest.summary:'No public note before this event.';
    $('#note-'+role).title=latest?`Public note · sequence ${latest.sequence}`:'No note published at this point';
    $('#station-'+role).dataset.active=String(record.speaker===role); $$(`[data-role-note="${role}"]`).forEach(button=>button.disabled=!latest);
  });
  $('#inspector').innerHTML=recordDetails(record);
  if($('#public-text')) $('#public-text').textContent=record.public_text;
  $('#raw-record').textContent=JSON.stringify(record,null,2);
  setLocation(record);
  announce(`Record ${position+1} of ${records.length}. ${labels[record.speaker]}. ${record.title}.`);
  if(position===records.length-1 && timer) { stop(); $('#replay-description').textContent='End of saved recording'; }
}
function centerSelectedRecord() { const list=$('#record-list'), selected=$('#record-list button[aria-current=true]'); if(list && selected){const delta=selected.getBoundingClientRect().top-list.getBoundingClientRect().top+list.scrollTop; list.scrollTop=Math.max(0,delta-list.clientHeight/2+selected.clientHeight/2);} }
window.addEventListener('resize',()=>{if(route()==='lab')requestAnimationFrame(centerSelectedRecord);});
function start() {
  if(records.length<2)return;
  if(position===records.length-1)selectRecord(0);
  $('#play').innerHTML=icon('pause'); $('#play').setAttribute('aria-label','Pause historical replay'); $('#play').setAttribute('aria-pressed','true');
  $('#replay-description').textContent='Playing saved records';
  timer=setInterval(()=>selectRecord(position+1),delay);setMotion();
}
function successView() {
  const successes=bundles.filter(b=>b.result.category==='success');
  let html=heading('Successful experiments.','Retained versions with linked checks and a comparable improvement. Every conclusion has a trail.') + partialNotice();
  html+=`<div class="archive-intro"><div><h2>What passed.<br>Why it matters.</h2><p>Success here means the final candidate was retained, its linked checks passed, and its comparison improved under reported matched conditions.</p></div><div class="archive-big-count">${pad(successes.length)}<small>VERIFIED &amp; RETAINED<br>IN THIS ARCHIVE READ</small></div></div>`;
  if(!successes.length) html+=`<div class="archive-empty">${icon('archive')}<h2>No supported success in this archive read.</h2><p>A completed experiment alone does not qualify.</p><a data-route href="${viewURL('lab')}" class="button">Browse the recorded process ${icon('arrow')}</a></div>`;
  successes.forEach(bundle=>{
    const result=bundle.result, comparison=result.decisiveComparison, cmp=comparison?.comparison;
    html+=`<article class="usefulness"><div class="usefulness-heading"><h2>Retired-key policy · experiment ${pad(bundle.number)}</h2>${status('success','Verified & retained')}</div><p>${esc(result.utility)}</p>${cmp?.metrics.map(m=>`<div class="compare-metric"><strong>${esc(m.baseline)}</strong>${icon('arrow')}<strong>${esc(m.candidate)}</strong><span>${esc(m.label)}<br>${esc(m.unit)} · ${result.candidateChecks.length} linked candidate checks passed</span></div>`).join('') || ''}<h3>What can this be used for?</h3><p>A retained candidate and its test evidence provide a documented reference for later migration-policy comparisons. The recorded checks make it possible to inspect which cases improved and trace the retained version back to its proposal.</p><div class="relationship-links">${reference(result.decisionRecord.record_id,'Open retention decision')}${comparison?reference(comparison.record_id,'Inspect before & after'):''}${reference(bundle.records.find(r=>r.speaker==='keeper')?.record_id,'Read the repair proposal')}</div><div class="utility-limit">${esc(result.limitations.join(' '))} This is an evidence-based explanation of the recorded result; the service supplies no separate deployment-use claim.</div><a href="${archiveURL(bundle)}" data-route class="text-link">Replay the complete experiment ${icon('arrow')}</a></article>`;
    html+=`<div class="panel-title"><h2>Checks behind this result</h2><span>${result.candidateChecks.length} linked records</span></div><ul class="evidence-list">${result.candidateChecks.map(r=>`<li><div class="archive-row-header">${reference(r.record_id,`#${r.sequence} · ${r.title}`)}${status(r.verification.outcome)}</div><span>${esc(r.verification.summary)}</span>${r.evidence.map(e=>{const href=safeEvidenceHref(e.href);return href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(e.label)} ${icon('external')}</a>`:`<p>${esc(e.label)} · Download not published</p>`}).join('')}</li>`).join('')}</ul>`;
  });
  app.innerHTML=html;
}
function archiveRow(bundle) { return `<li class="archive-row"><span class="archive-row-number">${pad(bundle.number)}</span><div><div class="archive-row-header"><h3>${esc(bundle.result.label)}</h3>${status(bundle.result.category,bundle.result.category==='no-discovery'?'No new finding':bundle.result.category==='error'?'Execution exception':bundle.result.category==='rejected'?'Not retained':'Evidence pending')}</div><p>${esc(bundle.result.reason)}</p><small>${esc(bundle.experiment.experiment_id)}<br>${bundle.records.length} PUBLIC RECORDS · RUN ${esc(bundle.experiment.state.toUpperCase())}</small></div><a href="${archiveURL(bundle)}" data-route class="text-link">Inspect trail ${icon('arrow')}</a></li>`; }
function renderArchiveRows() {
  const all=bundles.filter(b=>b.result.category!=='success');
  const shown=all.filter(b=>(filter==='all'||b.result.category===filter)&&(!search||JSON.stringify([b.experiment,b.result.reason,b.result.label,b.records.map(r=>r.public_text)]).toLowerCase().includes(search.toLowerCase())));
  $('#archive-results').innerHTML=shown.length?`<ol class="archive-list">${shown.map(archiveRow).join('')}</ol>`:`<div class="archive-empty">${icon('search')}<h2>No matching records.</h2><p>Try another term or reset the archive filters.</p><button class="button button-light" id="reset-filters">Reset filters</button></div>`;
  $('#shown-count').textContent=`${shown.length} of ${all.length} experiments`;
  $$('.filter').forEach(node=>node.setAttribute('aria-pressed',String(node.dataset.filter===filter)));
}
function failuresView() {
  const all=bundles.filter(b=>b.result.category!=='success');
  const categories=[['all','All outcomes'],['rejected','Rejected'],['no-discovery','No new finding'],['error','Exceptions'],['pending','Unresolved']].filter(([key])=>key!=='pending'||all.some(b=>b.result.category==='pending'));
  app.innerHTML=heading('Unsuccessful experiments.','Rejected ideas, uneventful searches and execution exceptions remain part of the research record.')+partialNotice()+`<div class="archive-intro"><div><h2>Keep the attempts.<br>Learn from the limits.</h2><p>An unsuccessful attempt can still document a useful boundary. A rejected repair, no new discrepancy and a stopped run mean different things.</p></div><div class="archive-big-count">${pad(all.length)}<small>WITHOUT A SUPPORTED<br>RETAINED IMPROVEMENT</small></div></div><div class="archive-notice">${icon('source')}<p><strong>Classification follows the final linked record.</strong> Partial metric improvements do not override a rejection. A search that finds no discrepancy does not prove there are no flaws. Execution errors do not establish a failed policy.</p></div><div class="archive-controls"><div class="filters" aria-label="Filter experiment outcomes">${categories.map(([key,title])=>`<button class="filter" data-filter="${key}" aria-pressed="${key===filter}">${title}<span>${key==='all'?all.length:all.filter(b=>b.result.category===key).length}</span></button>`).join('')}</div><label class="archive-search">${icon('search')}<input id="archive-search" type="search" placeholder="Search notes or experiment ID" aria-label="Search archived experiments" value="${esc(search)}"></label></div><div class="panel-title"><h2>Recorded attempts</h2><span id="shown-count"></span></div><div id="archive-results"></div>`;
  $('#archive-search').addEventListener('input',event=>{search=event.target.value;renderArchiveRows();});
  renderArchiveRows();
}
function exportArchive() {
  const payload = { exported_at: new Date().toISOString(), source: SOURCE_ORIGIN, service_status: archive ? archive.status : null,
    experiments_listed: archive ? archive.experiments.length : 0, experiments_classified: bundles.length,
    public_records: bundles.reduce((sum, bundle) => sum + bundle.records.length, 0),
    pages: archive ? archive.pages : null, complete: Boolean(archive && archive.complete),
    failed_reads: archive ? archive.failures : [],
    experiments: bundles.map(bundle => ({ experiment: bundle.experiment, records: bundle.records,
      classification: { category: bundle.result.category, label: bundle.result.label, reason: bundle.result.reason } })) };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = 'evorph-public-archive-read.json';
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  announce('Downloaded exactly the public records this page read.');
}
function provenanceView() {
  const evidence = bundles.flatMap(bundle => bundle.records.flatMap(record => record.evidence || []));
  const supplied = evidence.filter(item => safeEvidenceHref(item.href)).length;
  const wanted = new URLSearchParams(location.search).get('experiment');
  const example = bundles.find(bundle => bundle.experiment.experiment_id === wanted) || bundles[0];
  const pages = archive ? archive.pages : { experiments: 0, records: 0 };
  const readState = !archive ? 'No read has started.'
    : archive.error && !bundles.length ? 'The read failed before any experiment was classified.'
    : archive.complete ? 'Complete. ' + pages.experiments + ' experiment-list page(s) and ' + pages.records + ' record page(s) were followed until every cursor ended.'
    : 'Partial. ' + bundles.length + ' of ' + archive.experiments.length + ' experiments classified so far; the rest are still being read.';
  app.innerHTML = heading('Sources & method.', 'What this page read, how outcomes are grouped, and what it still cannot show.') + partialNotice() + `<div class="provenance-layout"><div class="provenance-main">
  <section><h2>Read live from the public archive.</h2><p>This page reads the same public endpoints the site already uses. It creates no identity, submits no challenge, calls no model and starts no research. Each page of results is validated with the site's own public contract before anything is displayed, and a first page is never treated as complete history.</p>
  <a class="source-link" href="${SOURCE_ORIGIN}/api/research/status" target="_blank" rel="noopener noreferrer">/api/research/status ${icon('external')}</a>
  <a class="source-link" href="${SOURCE_ORIGIN}/api/research/experiments?limit=100" target="_blank" rel="noopener noreferrer">/api/research/experiments?limit=100 ${icon('external')}</a>
  ${example ? `<a class="source-link" href="${sourceURL(example)}" target="_blank" rel="noopener noreferrer">/api/research/experiments/{id}/records?limit=100 ${icon('external')}</a>` : '<p>No experiment has been read yet, so no record endpoint can be named.</p>'}
  <p>${esc(readState)}</p></section>
  <section><h2>Public notes, visible work.</h2><p>SEEKER proposes a bounded challenge. KEEPER proposes a repair. The fixed verification program records checks and comparisons. Public statements are shown verbatim, with their original assertion status. They are not the models’ private internal reasoning.</p><p>The two computers are visual characters. Their highlights follow the selected speaker; their animation is not a signal that an AI is running now. A connected archive service is not evidence that a research round is in progress.</p></section>
  <section><h2>How an outcome earns its place.</h2><ul><li><strong>Successful:</strong> final program-authored retention, complete backward links, a matching candidate, passed linked checks with evidence references, and an improved comparison under service-reported matched conditions.</li><li><strong>Rejected:</strong> the final recorded candidate was rejected, including cases where a partial metric improved.</li><li><strong>No new finding:</strong> the completed experiment explicitly deferred because no verified discrepancy was found.</li><li><strong>Execution exception:</strong> the archived run failed. That alone says nothing about the policy’s correctness.</li><li><strong>Unresolved:</strong> missing or incomplete evidence, incomparable conditions, deferred or superseded conclusions, or an unfinished run.</li></ul><p>These classifications help navigate reported evidence. They do not independently verify the evidence contents or certify production security.</p></section>
  <section><h2>Evidence you can follow.</h2><p>Every event keeps its original sequence, record ID, source, version links and response relationships. Attachments use only the evidence href actually returned by the service. Missing links remain marked “Download not published.”</p><p>This read contains ${supplied} supplied evidence links and ${evidence.length - supplied} references without a usable link. Evidence packages stay on the archive and open separately; they have not been independently downloaded or validated by this page.</p></section>
  <section><h2>What a live view still needs.</h2><ul><li>Per-record publication times and step start/end events, to show true timing rather than a replay cadence.</li><li>A streaming or incremental run lifecycle with role activity, to distinguish queued work from work in progress.</li><li>Explicit outcome and bounded-use summaries with evidence references, to explain intended applications without inference.</li><li>Stable downloadable evidence for the older records whose href is empty.</li></ul><p>Records are mostly published after a round finishes, so this page replays a finished public record. It does not stream an experiment in progress.</p></section></div>
  <aside class="provenance-receipt"><h2>Read receipt</h2><dl class="receipt-list">
  <div><dt>Read started</dt><dd>${esc(capture())}</dd></div>
  <div><dt>Read finished</dt><dd>${archive && archive.finished_at ? esc(formatUTC(archive.finished_at)) : 'Still reading'}</dd></div>
  <div><dt>Source</dt><dd>evorph.net public archive</dd></div>
  <div><dt>Archive mode</dt><dd>${esc(archive && archive.status ? archive.status.mode : 'unknown')}</dd></div>
  <div><dt>Experiments listed</dt><dd>${archive ? archive.experiments.length : 0}</dd></div>
  <div><dt>Experiments classified</dt><dd>${bundles.length}</dd></div>
  <div><dt>Public records read</dt><dd>${bundles.reduce((sum, bundle) => sum + bundle.records.length, 0)}</dd></div>
  <div><dt>Pages followed</dt><dd>${pages.experiments} list · ${pages.records} record</dd></div>
  <div><dt>Failed record reads</dt><dd>${archive ? archive.failures.length : 0}</dd></div>
  <div><dt>Schema</dt><dd>public-research.v2</dd></div>
  <div><dt>Service at read time</dt><dd>${archive && archive.status && archive.status.connected ? 'Connected' : 'Not connected'}<br>Not agent activity</dd></div></dl>
  <button class="button" id="export-archive">${icon('download')} Download what this page read</button>
  <p>The export holds exactly the public records this page received, with their original fields, plus this receipt. It holds no private state and no inference.</p></aside></div>`;
}
function renderRoute() {
  if(!archive || !archive.read_at)return;
  const page=route();
  $$('.navigation a').forEach(node=>{const active=node.dataset.page===page;node.classList.toggle('active',active);if(active)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  $('#breadcrumb').textContent=`Workspace / ${routeNames[page]}`;
  document.title=`EVORPH — ${routeNames[page]}`;
  if(page==='success')successView();else if(page==='failures')failuresView();else if(page==='provenance')provenanceView();else renderLab();
}
document.addEventListener('click',event=>{
  if(event.target.closest('#export-archive')){exportArchive();return;}
  const anchor=event.target.closest('a[data-route]');
  if(anchor && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button===0){event.preventDefault();navigate(anchor.href);return;}
  const roleNote=event.target.closest('[data-role-note]'); if(roleNote){const latest=records.slice(0,position+1).filter(r=>r.speaker===roleNote.dataset.roleNote).at(-1);if(latest){stop();selectRecord(records.indexOf(latest));$('#inspector').focus({preventScroll:true});}return;}
  const ref=event.target.closest('[data-record]');
  if(ref){const found=byRecord.get(ref.dataset.record);if(found){if(route()==='lab'&&found.bundle===current){stop();selectRecord(records.findIndex(r=>r.record_id===found.record.record_id));$('#inspector').focus?.({preventScroll:true});}else navigate(recordURL(found.bundle,found.record));}return;}
  const trail=event.target.closest('[data-position]');if(trail){stop();selectRecord(Number(trail.dataset.position),false);return;}
  const choice=event.target.closest('[data-filter]');if(choice){filter=choice.dataset.filter;renderArchiveRows();return;}
  const id=event.target.closest('button')?.id;
  if(id==='previous'){stop();selectRecord(position-1);}else if(id==='next'){stop();selectRecord(position+1);}else if(id==='play'){if(timer){stop();$('#replay-description').textContent='Saved records · paused';}else start();}else if(id==='motion-toggle'){motion=!motion;event.target.closest('button').setAttribute('aria-pressed',String(!motion));event.target.closest('button').innerHTML=icon(motion?'pause':'play')+`<span>${motion?'Pause motion':'Enable motion'}</span>`;setMotion();}else if(id==='reset-filters'){filter='all';search='';$('#archive-search').value='';renderArchiveRows();}
});
window.addEventListener('popstate',()=>{stop();renderRoute();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
function registerBundle(bundle) {
  const index = archive.experiments.findIndex(item => item.experiment_id === bundle.experiment.experiment_id);
  const entry = { ...bundle, number: index >= 0 ? index + 1 : bundles.length + 1, result: classifyExperiment(bundle) };
  bundles.push(entry);
  orderedRecords(entry).forEach(record => {
    byRecord.set(record.record_id, { bundle: entry, record });
    if (record.candidate && record.candidate.version_id && !byVersion.has(record.candidate.version_id)) {
      byVersion.set(record.candidate.version_id, { bundle: entry, record });
    }
  });
  return entry;
}
function updateReceipt() {
  $('#total-experiments').textContent = String(archive.experiments.length);
  $('#total-records').textContent = String(bundles.reduce((sum, bundle) => sum + bundle.records.length, 0));
  const success = bundles.filter(bundle => bundle.result.category === 'success').length;
  $('#success-count').textContent = String(success);
  $('#failure-count').textContent = String(bundles.length - success);
  const badge = $('#source-state');
  if (badge) badge.textContent = archive.error && !bundles.length ? 'Archive read failed'
    : archive.complete ? 'Archive read complete · ' + bundles.length + ' experiments'
    : archive.failures.length ? 'Partial read · ' + bundles.length + ' of ' + archive.experiments.length
    : 'Reading archive · ' + bundles.length + ' of ' + archive.experiments.length;
}
function keepFocus(render) {
  const active = document.activeElement;
  const id = active && active.id ? active.id : null;
  const caret = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  render();
  if (!id) return;
  const node = document.getElementById(id);
  if (!node) return;
  node.focus({ preventScroll: true });
  if (caret !== null) { try { node.setSelectionRange(caret, caret); } catch { /* not a text field */ } }
}
function refreshAfterProgress() {
  if (!archive) return;
  const page = route();
  if (page === 'lab') {
    const requested = new URLSearchParams(location.search).get('experiment');
    const target = requested || (bundles[0] ? bundles[0].experiment.experiment_id : null);
    if (target && renderedLab !== target && bundles.some(bundle => bundle.experiment.experiment_id === target)) renderRoute();
    return;
  }
  if (page === 'success' || page === 'failures') keepFocus(renderRoute);
}
function renderFatal(message) {
  app.innerHTML = `<div class="error-state"><h1>The archive could not be read.</h1><p>${esc(message)} Nothing was classified, so no outcome is claimed either way.</p><button class="button" id="retry">Read the archive again ${icon('arrow')}</button></div>`;
  $('#retry').addEventListener('click', () => { app.hidden = true; $('#loading-state').hidden = false; load(); });
}
async function load() {
  archive = { status: null, experiments: [], pages: { experiments: 0, records: 0 }, failures: [],
              complete: false, error: null, read_at: null, finished_at: null };
  bundles = []; current = null; records = []; position = 0; renderedLab = null;
  byRecord.clear(); byVersion.clear();
  try {
    archive.status = await reader.readStatus();
    const list = await reader.listExperiments(archive.status.mode);
    archive.experiments = list.experiments;
    archive.pages.experiments = list.pages;
    archive.read_at = new Date().toISOString();
    $('#loading-state').hidden = true; app.hidden = false;
    updateReceipt(); renderRoute();
    for (const item of list.experiments) {
      try {
        const bundle = await reader.readRecords(item.experiment_id, archive.status.mode);
        archive.pages.records += bundle.pages;
        registerBundle(bundle);
      } catch (error) {
        archive.failures.push({ experiment_id: item.experiment_id, message: error.message });
      }
      updateReceipt(); refreshAfterProgress();
    }
    archive.complete = archive.failures.length === 0;
  } catch (error) {
    archive.error = error.message || 'The archive could not be read.';
  }
  archive.finished_at = new Date().toISOString();
  $('#loading-state').hidden = true; app.hidden = false;
  if (!bundles.length && archive.error) { renderFatal(archive.error); return; }
  updateReceipt(); renderRoute();
}
load();
