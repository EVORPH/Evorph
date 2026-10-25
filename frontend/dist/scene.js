import * as THREE from './assets/vendor/three.module.js';

// An authored visual metaphor for the recorded research stages.
// Geometry and motion are presentation, never model telemetry.
const PHASES = ['explore', 'counterexample', 'repair', 'verify', 'archive'];
const clamp = (n, low = 0, high = 1) => Math.min(high, Math.max(low, n));

class FilamentCurve extends THREE.Curve {
  constructor(index, total) {
    super();
    this.u = (index / Math.max(1, total - 1)) * 2 - 1;
  }
  getPoint(t, target = new THREE.Vector3()) {
    const u = this.u;
    const a = -1.51 + t * 4.30;
    const opening = Math.sin(Math.PI * t);
    const radius = 1.13 + u * 0.23 * opening;
    return target.set(
      -radius * Math.cos(a) - 0.20 + 0.16 * Math.sin(a * 1.7),
      1.68 * Math.sin(a) - 0.10 + u * 0.105 * opening,
      0.48 * Math.sin(a * 1.34) + u * 0.50 * opening + 0.23,
    );
  }
}

// A closed, gently twisted lamella: thickness produces real edge highlights.
function makeLamella(index, total) {
  const segments = 84;
  const positions = [];
  const indices = [];
  const u = (index / Math.max(1, total - 1)) * 2 - 1;
  const width = 0.082 + 0.016 * Math.cos(u * Math.PI * 0.5);
  const thickness = 0.026;
  for (let i = 0; i <= segments; i++) {
    const t = i / segments;
    const a = -1.74 + t * 4.55;
    const taper = 0.42 + 0.58 * Math.pow(Math.sin(Math.PI * t), 0.32);
    const r = 1.06 + 0.115 * Math.sin(a * 1.45) + u * 0.075;
    const twist = a * 0.60 + 0.25;
    for (const [side, face] of [[-1, 1], [1, 1], [-1, -1], [1, -1]]) {
      const w = side * width * taper;
      const n = face * thickness;
      positions.push(
        0.32 + (r + w * Math.cos(twist)) * Math.cos(a) + n * Math.sin(a),
        (1.70 + w * Math.cos(twist)) * Math.sin(a) + n * Math.cos(a),
        -0.40 * Math.sin(a * 1.3) + u * 0.74 + w * Math.sin(twist) + n,
      );
    }
    if (i < segments) {
      const p = i * 4;
      // Front, rear, and the two long edges.
      for (const [a0, a1, b0, b1] of [
        [0, 1, 4, 5], [3, 2, 7, 6], [2, 0, 6, 4], [1, 3, 5, 7],
      ]) {
        indices.push(p + a0, p + b0, p + a1, p + a1, p + b0, p + b1);
      }
    }
  }
  indices.push(0, 1, 2, 1, 3, 2);
  const e = segments * 4;
  indices.push(e, e + 2, e + 1, e + 1, e + 2, e + 3);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  geometry.computeBoundingSphere();
  return geometry;
}

function createSoftShadow() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  const gradient = ctx.createRadialGradient(64, 64, 5, 64, 64, 64);
  gradient.addColorStop(0, 'rgba(39,33,24,0.19)');
  gradient.addColorStop(0.38, 'rgba(39,33,24,0.10)');
  gradient.addColorStop(1, 'rgba(39,33,24,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(canvas);
}

function inertScene(container) {
  container.classList.add('scene-fallback');
  container.classList.remove('scene-ready');
  container.dataset.scene = 'fallback';
  return {
    setPhase(index) { container.dataset.phase = PHASES[clamp(Math.round(index) || 0, 0, 4)]; },
    setMode(mode) { container.dataset.sceneMode = mode; },
    setProgress(value) { container.dataset.sceneProgress = String(clamp(Number(value) || 0)); },
    setPaused(value) { container.dataset.scenePaused = String(Boolean(value)); },
    dispose() {},
  };
}

/**
 * createResearchScene(element, {variant: 'hero'|'lab', reducedMotion: boolean})
 *
 * Phase: 0 explore, 1 counterexample, 2 repair, 3 verify, 4 archive.
 * Progress is a caller-owned presentation cursor, not a measured success score.
 * Uses vendored Three.js only; no remote assets, model calls or network requests.
 */
export function createResearchScene(container, {
  variant = 'hero',
  reducedMotion = false,
} = {}) {
  if (!container || typeof container.appendChild !== 'function') {
    throw new TypeError('A scene container element is required.');
  }
  const dark = variant === 'lab';
  const palette = {
    accent: 0xff5b37,
    porcelain: dark ? 0xe6dfcd : 0xf3efe5,
    metal: dark ? 0xa89e87 : 0x958a76,
    fine: dark ? 0xb1a48b : 0x6b6253,
    backdrop: 0x141713,
  };
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      alpha: !dark,
      antialias: true,
      powerPreference: 'low-power',
      preserveDrawingBuffer: false,
      failIfMajorPerformanceCaveat: false,
    });
  } catch {
    return inertScene(container);
  }

  const canvas = renderer.domElement;
  canvas.className = 'research-scene-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.cssText = 'display:block;width:100%;height:100%;pointer-events:none;';
  renderer.setClearColor(dark ? palette.backdrop : 0xefeee8, dark ? 1 : 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = dark ? 1.12 : 1.22;
  container.appendChild(canvas);
  container.classList.remove('scene-fallback');

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-3, 3, 2.6, -2.6, 0.1, 60);
  camera.position.set(4.5, 2.25, 9.5);
  camera.lookAt(0, -0.02, 0);
  scene.add(new THREE.HemisphereLight(0xfff9ed, dark ? 0x393e31 : 0x8d8476, dark ? 1.65 : 2.6));
  const key = new THREE.DirectionalLight(0xfff8ed, dark ? 4.2 : 3.6);
  key.position.set(-3.2, 6, 4.5);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xffffff, dark ? 3.0 : 2.5);
  rim.position.set(4, 2, -3);
  scene.add(rim);
  const fill = new THREE.DirectionalLight(0xc6b697, dark ? 1.2 : 0.65);
  fill.position.set(-4, -2, 2);
  scene.add(fill);

  const sculpture = new THREE.Group();
  sculpture.rotation.set(0.02, -0.23, -0.30);
  scene.add(sculpture);
  const seeker = new THREE.Group();
  const keeper = new THREE.Group();
  sculpture.add(seeker, keeper);

  const porcelain = new THREE.MeshPhysicalMaterial({
    color: palette.porcelain,
    roughness: 0.30,
    metalness: 0.16,
    clearcoat: 0.45,
    clearcoatRoughness: 0.30,
    side: THREE.DoubleSide,
  });
  const metal = new THREE.MeshStandardMaterial({
    color: palette.metal, metalness: 0.78, roughness: 0.27,
  });
  const orange = new THREE.MeshStandardMaterial({
    color: palette.accent, metalness: 0.28, roughness: 0.34,
  });
  const neutralWire = new THREE.MeshStandardMaterial({
    color: palette.fine, metalness: 0.55, roughness: 0.36,
  });
  const edgeMaterial = new THREE.LineBasicMaterial({
    color: dark ? 0xdfd1b7 : 0x6c6251, transparent: true, opacity: dark ? 0.3 : 0.16,
  });
  const accentLine = new THREE.LineBasicMaterial({
    color: palette.accent, transparent: true, opacity: dark ? 0.42 : 0.32,
  });

  // The Keeper is built from offset ceramic blades, with two precise metal seams.
  const blades = [];
  const bladeCount = dark ? 11 : 13;
  for (let i = 0; i < bladeCount; i++) {
    const mesh = new THREE.Mesh(
      makeLamella(i, bladeCount),
      i === 2 || i === bladeCount - 3 ? metal : porcelain,
    );
    mesh.userData.index = i;
    keeper.add(mesh);
    blades.push(mesh);
  }

  // The Seeker is an open, tensioned bundle. Every line has a deliberate path.
  const filaments = [];
  const filamentCount = dark ? 21 : 27;
  const paths = [];
  const terminalGeometry = new THREE.CylinderGeometry(0.019, 0.019, 0.11, 6);
  const terminals = new THREE.InstancedMesh(terminalGeometry, metal, filamentCount * 2);
  terminals.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  terminals.frustumCulled = false;
  seeker.add(terminals);
  const terminalMatrices = [];
  const terminalMatrix = new THREE.Matrix4();
  const yAxis = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i < filamentCount; i++) {
    const path = new FilamentCurve(i, filamentCount);
    paths.push(path);
    const group = new THREE.Group();
    const wire = new THREE.Mesh(
      new THREE.TubeGeometry(path, 88, i % 6 === 0 ? 0.0155 : 0.009, 5, false),
      i % 6 === 0 ? neutralWire : orange,
    );
    group.add(wire);
    for (const endpoint of [0, 1]) {
      terminalMatrices.push(new THREE.Matrix4().compose(
        path.getPoint(endpoint),
        new THREE.Quaternion().setFromUnitVectors(yAxis, path.getTangent(endpoint).normalize()),
        new THREE.Vector3(1, 1, 1),
      ));
    }
    group.userData.index = i;
    filaments.push(group);
    seeker.add(group);
  }

  // Finite tracing guides reinforce the open construction rather than orbiting it.
  for (let j = 0; j < 3; j++) {
    const points = [];
    for (let i = 0; i <= 64; i++) {
      const t = i / 64;
      const a = -1.38 + t * 3.8;
      points.push(new THREE.Vector3(
        -1.56 * Math.cos(a) - 0.19,
        1.93 * Math.sin(a) - 0.10,
        0.4 * Math.sin(a * 1.3) + (j - 1) * 0.30,
      ));
    }
    seeker.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), j === 1 ? accentLine : edgeMaterial));
  }

  const cursor = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.07, 0.075), metal);
  seeker.add(cursor);
  const cursorCurve = paths[Math.floor(paths.length / 2)];
  const cursorPoint = new THREE.Vector3();
  const tangent = new THREE.Vector3();

  // A small assembly of cross-pins provides scale and material contrast.
  const pins = new THREE.Group();
  const pinGeometry = new THREE.CylinderGeometry(0.024, 0.024, 1.60, 8);
  for (let i = 0; i < 3; i++) {
    const pin = new THREE.Mesh(pinGeometry, metal);
    pin.rotation.x = Math.PI * 0.5;
    pin.position.set(0.93 + i * 0.075, 0.40 + i * 0.12, -0.10);
    pins.add(pin);
  }
  keeper.add(pins);

  let shadowTexture = null;
  let shadow = null;
  if (!dark) {
    shadowTexture = createSoftShadow();
    if (shadowTexture) {
      shadow = new THREE.Mesh(
        new THREE.PlaneGeometry(5, 3),
        new THREE.MeshBasicMaterial({
          map: shadowTexture, transparent: true, depthWrite: false, opacity: 0.62,
        }),
      );
      shadow.rotation.x = -Math.PI * 0.5;
      shadow.position.y = -2.22;
      scene.add(shadow);
    }
  }

  const motionMedia = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const hoverMedia = window.matchMedia?.('(hover: hover) and (pointer: fine)');
  let reduce = Boolean(reducedMotion || motionMedia?.matches);
  let paused = false;
  let destroyed = false;
  let contextLost = false;
  let visible = true;
  let frame = 0;
  let lastTime = 0;
  let lastDraw = 0;
  let elapsed = 0;
  let phase = 0;
  let mode = 'both';
  let progress = 0.32;
  let currentProgress = progress;
  let width = 0;
  let height = 0;
  let pose = 0;
  let targetPose = 0;
  let pointerX = 0;
  let pointerY = 0;
  let tiltX = 0;
  let tiltY = 0;

  const settings = [
    { expansion: 1, traceX: -0.12, traceScale: 1.03, bladeFan: 0.12, turn: -0.16 },
    { expansion: 0.12, traceX: 0.14, traceScale: 0.88, bladeFan: 0.03, turn: -0.03 },
    { expansion: 0.65, traceX: -0.03, traceScale: 0.94, bladeFan: 1, turn: 0.20 },
    { expansion: 0, traceX: 0.18, traceScale: 0.92, bladeFan: 0, turn: 0.30 },
    { expansion: 0.04, traceX: 0.12, traceScale: 0.94, bladeFan: 0.04, turn: 0.24 },
  ];

  function refreshMetadata() {
    container.dataset.phase = PHASES[phase];
    container.dataset.sceneMode = mode;
    container.dataset.sceneProgress = String(progress);
    container.dataset.scenePaused = String(paused || reduce);
  }

  function update(dt, instant = false) {
    const ease = instant ? 1 : 1 - Math.exp(-dt * 7);
    pose += (targetPose - pose) * ease;
    currentProgress += (progress - currentProgress) * (instant ? 1 : 1 - Math.exp(-dt * 11));
    tiltX += ((reduce ? 0 : pointerX) - tiltX) * ease;
    tiltY += ((reduce ? 0 : pointerY) - tiltY) * ease;
    const p0 = Math.floor(pose);
    const p1 = Math.min(settings.length - 1, p0 + 1);
    const part = pose - p0;
    const a = settings[p0];
    const b = settings[p1];
    const mix = (key) => THREE.MathUtils.lerp(a[key], b[key], part);
    const spread = mix('expansion');
    const fan = mix('bladeFan');
    const ambient = !reduce && !paused && phase !== 4;
    const breath = ambient ? Math.sin(elapsed * 0.43) : 0;
    sculpture.rotation.x = 0.02 + tiltY * 0.055 + breath * 0.014;
    sculpture.rotation.y = -0.23 + mix('turn') + tiltX * 0.11;
    sculpture.rotation.z = -0.30 + (ambient ? Math.sin(elapsed * 0.29) * 0.012 : 0);
    sculpture.position.y = breath * 0.025;
    seeker.position.set(mix('traceX'), spread * 0.07, 0.08 + spread * 0.06);
    seeker.scale.set(mix('traceScale'), 1 + spread * 0.055, 1 + spread * 0.12);
    keeper.rotation.y = fan * 0.14;
    for (let i = 0; i < blades.length; i++) {
      const u = i / (blades.length - 1) - 0.5;
      blades[i].position.set(u * fan * 0.25, Math.abs(u) * fan * 0.12, u * (spread * 0.17 + fan * 0.45));
      blades[i].rotation.z = u * fan * 0.27;
      blades[i].rotation.y = u * fan * 0.18;
    }
    for (let i = 0; i < filaments.length; i++) {
      const u = i / (filaments.length - 1) - 0.5;
      filaments[i].rotation.z = u * spread * 0.09;
      filaments[i].position.z = u * spread * 0.18;
      filaments[i].updateMatrix();
      for (let endpoint = 0; endpoint < 2; endpoint++) {
        const slot = i * 2 + endpoint;
        terminalMatrix.multiplyMatrices(filaments[i].matrix, terminalMatrices[slot]);
        terminals.setMatrixAt(slot, terminalMatrix);
      }
    }
    terminals.instanceMatrix.needsUpdate = true;
    cursor.position.copy(cursorCurve.getPoint(clamp(currentProgress), cursorPoint));
    tangent.copy(cursorCurve.getTangent(clamp(currentProgress))).normalize();
    cursor.quaternion.setFromUnitVectors(yAxis, tangent);
    seeker.visible = mode !== 'keeper';
    keeper.visible = mode !== 'seeker';
    const focus = mode === 'seeker' ? 0.20 : mode === 'keeper' ? -0.15 : 0;
    sculpture.position.x = focus;
    if (shadow) shadow.material.opacity = mode === 'both' ? 0.62 : 0.42;
  }

  function draw() {
    if (!destroyed && !contextLost && visible && !document.hidden && width > 0 && height > 0) {
      renderer.render(scene, camera);
    }
  }

  function shouldRun() {
    return !destroyed && !contextLost && visible && !document.hidden && !reduce && !paused;
  }

  function tick(now) {
    frame = 0;
    if (!shouldRun()) return;
    const dt = lastTime ? Math.min(0.05, (now - lastTime) / 1000) : 1 / 60;
    lastTime = now;
    elapsed += dt;
    // Two scenes may share a page. Cap their render cadence at 30 fps.
    if (now - lastDraw >= 32) {
      update(Math.max(dt, 1 / 30));
      draw();
      lastDraw = now;
    }
    const settled = Math.abs(pose - targetPose) < 0.0002
      && Math.abs(currentProgress - progress) < 0.0002
      && Math.abs(tiltX - pointerX) < 0.0002
      && Math.abs(tiltY - pointerY) < 0.0002;
    // Archive settles to a still view instead of looping an activity animation.
    if (phase !== 4 || !settled) frame = requestAnimationFrame(tick);
  }

  function wake() {
    if (!shouldRun()) {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      lastTime = 0;
      return;
    }
    if (!frame) {
      lastTime = 0;
      frame = requestAnimationFrame(tick);
    }
  }

  function commit() {
    refreshMetadata();
    if (reduce || paused || !visible || document.hidden) update(0, true);
    else update(1 / 60);
    draw();
    wake();
  }

  function resize() {
    if (destroyed) return;
    const rect = container.getBoundingClientRect();
    width = Math.max(1, Math.round(rect.width));
    height = Math.max(1, Math.round(rect.height));
    const aspect = width / height;
    const span = Math.max(dark ? 4.68 : 4.90, 4.25 / aspect);
    camera.left = -span * aspect / 2;
    camera.right = span * aspect / 2;
    camera.top = span / 2;
    camera.bottom = -span / 2;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, width < 600 ? 1.25 : 1.5));
    renderer.setSize(width, height, false);
    update(0, true);
    draw();
  }

  function pointerMove(event) {
    if (reduce || paused || event.pointerType === 'touch' || !hoverMedia?.matches) return;
    const rect = container.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    pointerX = clamp((event.clientX - rect.left) / rect.width, 0, 1) * 2 - 1;
    pointerY = clamp((event.clientY - rect.top) / rect.height, 0, 1) * 2 - 1;
    wake();
  }

  function pointerLeave() {
    pointerX = pointerY = 0;
    wake();
  }

  function motionChanged() {
    reduce = Boolean(reducedMotion || motionMedia?.matches);
    if (reduce) pointerX = pointerY = 0;
    commit();
  }

  function visibilityChanged() {
    if (!document.hidden && visible) {
      draw();
      wake();
    } else {
      wake();
    }
  }

  function lost(event) {
    event.preventDefault();
    contextLost = true;
    container.classList.add('scene-fallback');
    container.classList.remove('scene-ready');
    container.dataset.scene = 'fallback';
    canvas.style.visibility = 'hidden';
    wake();
  }

  function restored() {
    contextLost = false;
    container.classList.remove('scene-fallback');
    container.classList.add('scene-ready');
    container.dataset.scene = 'webgl';
    canvas.style.visibility = '';
    resize();
    wake();
  }

  const resizeObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
  resizeObserver?.observe(container);
  if (!resizeObserver) window.addEventListener('resize', resize);
  const intersectionObserver = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) draw();
      wake();
    }, { rootMargin: '60px' })
    : null;
  intersectionObserver?.observe(container);
  container.addEventListener('pointermove', pointerMove, { passive: true });
  container.addEventListener('pointerleave', pointerLeave, { passive: true });
  document.addEventListener('visibilitychange', visibilityChanged);
  motionMedia?.addEventListener?.('change', motionChanged);
  canvas.addEventListener('webglcontextlost', lost);
  canvas.addEventListener('webglcontextrestored', restored);
  refreshMetadata();
  resize(); // Initial geometry is visible synchronously, before any animation frame.
  container.classList.add('scene-ready');
  container.dataset.scene = 'webgl';
  wake();

  return {
    setPhase(index) {
      if (destroyed) return;
      phase = clamp(Math.round(Number(index) || 0), 0, 4);
      targetPose = phase;
      commit();
    },
    setMode(next) {
      if (destroyed || !['both', 'seeker', 'keeper'].includes(next)) return;
      mode = next;
      commit();
    },
    setProgress(value) {
      if (destroyed) return;
      progress = clamp(Number(value) || 0);
      commit();
    },
    setPaused(value) {
      if (destroyed) return;
      paused = Boolean(value);
      commit();
    },
    dispose() {
      if (destroyed) return;
      destroyed = true;
      cancelAnimationFrame(frame);
      resizeObserver?.disconnect();
      intersectionObserver?.disconnect();
      window.removeEventListener('resize', resize);
      container.removeEventListener('pointermove', pointerMove);
      container.removeEventListener('pointerleave', pointerLeave);
      document.removeEventListener('visibilitychange', visibilityChanged);
      motionMedia?.removeEventListener?.('change', motionChanged);
      canvas.removeEventListener('webglcontextlost', lost);
      canvas.removeEventListener('webglcontextrestored', restored);
      const geometries = new Set();
      const materials = new Set();
      scene.traverse((object) => {
        if (object.geometry) geometries.add(object.geometry);
        if (object.material) {
          for (const material of [].concat(object.material)) materials.add(material);
        }
      });
      geometries.forEach((geometry) => geometry.dispose());
      materials.forEach((material) => material.dispose());
      shadowTexture?.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
      container.classList.remove('scene-ready', 'scene-fallback');
      container.dataset.scene = 'disposed';
    },
  };
}
