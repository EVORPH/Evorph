// Authored native-canvas motion inspired by the inspected references.
// These are explanatory/brand animations, never research telemetry.
const TAU = Math.PI * 2;
const clamp = (value, min=0, max=1) => Math.max(min, Math.min(max, value));
class CanvasSurface {
  constructor(canvas, draw) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.draw = draw;
    this.time = 0;
    this.width = 0;
    this.height = 0;
    this.paused = true;
    this.visible = true;
    this.running = false;
    this.raf = 0;
    this.lastFrame = 0;
    this.previousTime = 0;
    this.tick = this.tick.bind(this);
    this.resize = this.resize.bind(this);
    if (!this.ctx) return;
    this.resizeObserver = new ResizeObserver(this.resize);
    this.resizeObserver.observe(canvas);
    this.intersection = new IntersectionObserver(entries => {
      this.visible = entries[0].isIntersecting;
      this.sync();
    }, {rootMargin: '80px'});
    this.intersection.observe(canvas);
    this.onVisibility = () => this.sync();
    document.addEventListener('visibilitychange', this.onVisibility);
    this.resize();
  }
  resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;
    const dpr = Math.min(devicePixelRatio || 1, 1.5);
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this.ctx?.setTransform(dpr,0,0,dpr,0,0);
    this.paint();
  }
  paint() {
    if (!this.ctx || !this.width || !this.height) return;
    this.ctx.clearRect(0,0,this.width,this.height);
    this.draw(this.ctx,this.width,this.height,this.time);
  }
  tick(now) {
    if (!this.running) return;
    if (now - this.lastFrame >= 32) {
      this.time += Math.min((now - (this.previousTime || now)) / 1000, .1);
      this.lastFrame = now;
      this.previousTime = now;
      this.paint();
    }
    this.raf = requestAnimationFrame(this.tick);
  }
  sync() {
    const shouldRun = !this.paused && this.visible && !document.hidden && !!this.ctx;
    if (shouldRun === this.running) { if (!shouldRun) this.paint(); return; }
    this.running = shouldRun;
    this.canvas.dataset.animation = shouldRun ? 'running' : 'still';
    if (shouldRun) { this.previousTime = 0; this.raf = requestAnimationFrame(this.tick); }
    else { cancelAnimationFrame(this.raf); this.paint(); }
  }
  setPaused(value) { this.paused = value; this.sync(); }
  invalidate() { this.paint(); }
  dispose() {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.resizeObserver?.disconnect();
    this.intersection?.disconnect();
    document.removeEventListener('visibilitychange',this.onVisibility);
  }
}
function path(ctx, points, color, width=1) {
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  if (points.length === 4) ctx.bezierCurveTo(...points[1],...points[2],...points[3]);
  else points.slice(1).forEach(point => ctx.lineTo(...point));
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
}
function cubic(points, t) {
  const n = 1-t;
  return [n*n*n*points[0][0] + 3*n*n*t*points[1][0] + 3*n*t*t*points[2][0] + t*t*t*points[3][0],
    n*n*n*points[0][1] + 3*n*n*t*points[1][1] + 3*n*t*t*points[2][1] + t*t*t*points[3][1]];
}
function packet(ctx, points, progress, color, radius=2) {
  const [x,y]=cubic(points,progress);
  ctx.beginPath();ctx.arc(x,y,radius,0,TAU);ctx.fillStyle=color;ctx.fill();
}
export function createHeroField(canvas, stage, tilt) {
  const pointer={x:0,y:0,targetX:0,targetY:0};
  let paused=true;
  const surface = new CanvasSurface(canvas,(ctx,w,h,time)=>{
    pointer.x += (pointer.targetX-pointer.x)*.07;
    pointer.y += (pointer.targetY-pointer.y)*.07;
    const mobile=w<641;
    const cx=w*(mobile?.5:.76)+pointer.x*13;
    const cy=h*(mobile?.68:.48)+pointer.y*10;
    for(let i=0;i<(mobile?28:60);i++) {
      const x=((i*137.3+Math.sin(time*.17+i)*23)%w+w)%w;
      const y=((i*91.7-time*(2+i%4))%h+h)%h;
      const alpha=.12+.15*(.5+.5*Math.sin(time*.7+i));
      ctx.fillStyle='rgba(187,221,137,'+alpha+')';
      ctx.fillRect(x,y,i%5===0?2:1,i%5===0?2:1);
    }
    ctx.save();
    for(let i=0;i<(mobile?9:16);i++){
      const startX=w*(.33+i*.055);
      const points=[[startX,h+50],[startX-w*.18,h*.75],[cx+w*.22*Math.sin(i*.55),cy+h*.23],[cx+Math.sin(i*.9)*w*.13,cy-h*.23]];
      path(ctx,points,'rgba(151,196,65,'+(i%3===0?.12:.048)+')',i%4===0?1:.6);
      for(let j=0;j<2;j++){
        const t=(time*.045+i*.12+j*.5)%1;
        const [x,y]=cubic(points,t);
        const trail=cubic(points,Math.max(0,t-.045));
        const gradient=ctx.createLinearGradient(trail[0],trail[1],x,y);
        gradient.addColorStop(0,'rgba(197,250,39,0)');
        gradient.addColorStop(1,'rgba(197,250,39,.65)');
        path(ctx,[trail,[x,y]],gradient,1.4);
        ctx.fillStyle='#c5fa27';ctx.fillRect(x-1,y-1,2,2);
      }
    }
    ctx.translate(cx,cy);
    ctx.rotate(-.38+Math.sin(time*.1)*.05);
    for(let ring=0;ring<3;ring++){
      ctx.beginPath();ctx.ellipse(0,0,w*(mobile?.38:.21)+ring*17,h*.14+ring*12,0,0,TAU);
      ctx.strokeStyle=ring===1?'rgba(164,200,83,.14)':'rgba(164,200,83,.06)';ctx.lineWidth=.7;ctx.stroke();
      const a=time*(.12+ring*.03)+ring*2.1;
      const px=Math.cos(a)*(w*(mobile?.38:.21)+ring*17),py=Math.sin(a)*(h*.14+ring*12);
      ctx.fillStyle=ring===1?'#c5fa27':'#a8b891';ctx.fillRect(px-1.5,py-1.5,3,3);
    }
    ctx.restore();
    if(tilt && !paused) tilt.style.transform='rotateY('+(pointer.x*7)+'deg) rotateX('+(-pointer.y*5)+'deg)';
  });
  const onPointer=event=>{
    if(surface.paused)return;
    const rect=stage.getBoundingClientRect();
    pointer.targetX=clamp((event.clientX-rect.left)/rect.width,0,1)*2-1;
    pointer.targetY=clamp((event.clientY-rect.top)/rect.height,0,1)*2-1;
  };
  const reset=()=>{pointer.targetX=0;pointer.targetY=0;};
  stage.addEventListener('pointermove',onPointer);
  stage.addEventListener('pointerleave',reset);
  return {setPaused(value){paused=value;surface.setPaused(value);if(value&&tilt)tilt.style.transform='none';},dispose(){stage.removeEventListener('pointermove',onPointer);stage.removeEventListener('pointerleave',reset);surface.dispose();}};
}
export function createLabFlow(canvas) {
  let phase=0, role='both', scenario=0, outcome='pending';
  const surface = new CanvasSurface(canvas,(ctx,w,h,time)=>{
    const center=w*.5,y=h*.45,left=Math.min(105,w*.28),right=w-left,gate=w<400?31:39;
    const lime='#c5fa27',dim='rgba(154,183,112,.23)',error='#ff8d70';
    const topLeft=[[left,y-7],[left+35,y-50],[center-55,y-50],[center-gate,y-8]];
    const topRight=[[right,y-7],[right-35,y-50],[center+55,y-50],[center+gate,y-8]];
    const bottomLeft=[[left,y+17],[left+25,y+67],[center-55,y+65],[center-gate,y+13]];
    const bottomRight=[[right,y+17],[right-25,y+67],[center+55,y+65],[center+gate,y+13]];
    const fade=role==='keeper'?.28:1;
    ctx.globalAlpha=fade;
    path(ctx,topLeft,phase===2?error:phase>=3?'rgba(255,141,112,.35)':dim);
    path(ctx,bottomLeft,dim,.65);
    if(phase<3){packet(ctx,topLeft,(time*.28+.1)%1,phase===2?error:lime,2.5);}
    if(phase===1||phase===2){
      for(let i=0;i<6;i++) {
        const fork=[[left,y-15],[left+40,y-55-i*5],[center-68,y-65-i*8],[center-gate-5,y-20-i*5]];
        path(ctx,fork,'rgba(197,250,39,'+(.08-i*.009)+')',.7);
        packet(ctx,fork,(time*.2+i*.16)%1,phase===2&&i%2?error:'#8db735',1.5);
      }
    }
    if(phase>=3){
      const bx=center-gate-13,by=y-9;
      path(ctx,[[bx-4,by-5],[bx+4,by+3]],error,1.3);
      path(ctx,[[bx+4,by-5],[bx-4,by+3]],error,1.3);
      packet(ctx,topLeft,Math.min(.8,(time*.25)%1),error,2);
    }
    ctx.globalAlpha=role==='seeker'?.3:1;
    path(ctx,topRight,dim);
    path(ctx,bottomRight,phase>=3?lime:dim,phase>=4?1.5:1);
    if(phase>=3||phase===0)packet(ctx,bottomRight,(time*.22+.3)%1,phase>=3?lime:'#8a9b72',2.5);
    ctx.globalAlpha=1;
    if(['failed','error','unsupported','inconclusive','rejected'].includes(outcome)){
      ctx.save();ctx.setLineDash([3,4]);path(ctx,[[center,y+45],[center,y+76]],error,.8);ctx.restore();
      ctx.fillStyle=error;ctx.font='9px Plex,monospace';ctx.textAlign='center';ctx.fillText(outcome==='error'?'EXECUTION ERROR':outcome==='failed'?'CHECK FAILED':outcome.toUpperCase(),center,y+94);
    }
    if(outcome==='passed'||outcome==='retained'){
      const tail=[[center,y+45],[center,y+60],[center,y+76],[center,y+85]];
      path(ctx,tail,lime,1);
      if(phase===4)packet(ctx,tail,(time*.45)%1,lime,2.5);
      ctx.beginPath();ctx.arc(center,y+95,8,0,TAU);ctx.strokeStyle=lime;ctx.lineWidth=.8;ctx.stroke();
      path(ctx,[[center-3,y+95],[center-1,y+98],[center+4,y+92]],lime,1);
      if(phase===5){
        ctx.strokeStyle='rgba(197,250,39,.25)';ctx.beginPath();ctx.arc(center,y+95,14+Math.sin(time*1.2)*2,0,TAU);ctx.stroke();
      }
    }
    for(let i=0;i<3;i++){
      ctx.beginPath();ctx.arc(center,y,55+i*14,Math.PI*1.06,Math.PI*1.94);
      ctx.strokeStyle='rgba(161,190,117,'+(.06+i*.025)+')';ctx.lineWidth=.6;ctx.stroke();
    }
    canvas.dataset.phase=String(phase);
    canvas.dataset.scenario=String(scenario);
  });
  return {setPhase(value){phase=clamp(Math.round(value),0,5);surface.invalidate();},setOutcome(value){outcome=value;surface.invalidate();},setMode(value){role=value;surface.invalidate();},setCase(value){scenario=value;surface.invalidate();},setPaused(value){surface.setPaused(value);},dispose(){surface.dispose();}};
}
export function decryptText(element, value, reduced=false) {
  if (!element) return;
  if (element._decryptFrame) cancelAnimationFrame(element._decryptFrame);
  if (reduced) {element.textContent=value;return;}
  const symbols='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  let start;
  function frame(now) {
    start ??= now;
    const progress=clamp((now-start)/750);
    element.textContent=Array.from(value).map((char,i)=>i/value.length<progress||char===' '?char:symbols[Math.floor(Math.random()*symbols.length)]).join('');
    if(progress<1) element._decryptFrame=requestAnimationFrame(frame);
    else element.textContent=value;
  }
  element._decryptFrame=requestAnimationFrame(frame);
}
