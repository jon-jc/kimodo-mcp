import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// Model data only comes from the local application's allowlisted file routes.
export class MotionViewer {
  constructor(container, { onProgress, onLoaded, onError, onTime }) {
    this.container = container;
    this.callbacks = { onProgress, onLoaded, onError, onTime };
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(36, 1, 0.001, 1000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.25;
    this.renderer.domElement.setAttribute('aria-label', 'Interactive character model. Drag to orbit and scroll to zoom.');
    this.renderer.domElement.setAttribute('role', 'img');
    this.renderer.domElement.tabIndex = 0;
    container.prepend(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.95;
    this.scene.add(new THREE.HemisphereLight(0xf3eaff, 0x68748d, 2.1));
    const key = new THREE.DirectionalLight(0xffefdc, 3.4);
    key.position.set(3, 6, 5); this.scene.add(key);
    const rim = new THREE.DirectionalLight(0xb9b5ff, 2.8);
    rim.position.set(-4, 3, -3); this.scene.add(rim);
    this.grid = new THREE.GridHelper(10, 20, 0x626479, 0x42475c);
    this.grid.material.transparent = true; this.grid.material.opacity = 0.25;
    this.scene.add(this.grid);
    this.camera.position.set(2, 1.5, 3); this.controls.target.set(0, 0.8, 0);
    this.playing = false; this.time = 0; this.duration = 0; this.speed = 1; this.loadId = 0;
    this.wireframe = false; this.showSkeleton = false;
    this.observer = new ResizeObserver(() => this.resize()); this.observer.observe(container);
    this.resize();
    let previous = performance.now();
    this.renderer.setAnimationLoop(now => {
      const delta = Math.min(Math.max((now - previous) / 1000, 0), 0.1); previous = now;
      if (this.mixer && this.playing) {
        this.time += delta * this.speed;
        if (this.time >= this.duration) { this.time = this.duration; this.playing = false; }
        this.mixer.setTime(this.time); this.callbacks.onTime(this.time, this.duration, this.playing);
      }
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  resize() {
    const width = this.container.clientWidth, height = this.container.clientHeight;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / Math.max(height, 1); this.camera.updateProjectionMatrix();
  }

  disposeObject(object) {
    const textures = new Set(), materials = new Set(), geometries = new Set();
    object.traverse(node => {
      if (node.geometry) geometries.add(node.geometry);
      for (const material of [node.material].flat().filter(Boolean)) {
        materials.add(material);
        Object.values(material).forEach(value => { if (value?.isTexture) textures.add(value); });
      }
    });
    textures.forEach(texture => texture.dispose()); materials.forEach(material => material.dispose()); geometries.forEach(geometry => geometry.dispose());
  }

  async load(url) {
    // Never leave the previous candidate visible under a newly selected title
    // if loading the new candidate fails or is interrupted.
    this.clear();
    const id = ++this.loadId;
    this.callbacks.onProgress('Loading the character model…');
    this.playing = false;
    try {
      // A self-contained GLB needs no external resources. Block references to any
      // other origin in case an unexpected GLB contains external texture URIs.
      const manager = new THREE.LoadingManager();
      let resourceFailed = false;
      manager.onError = () => { resourceFailed = true; };
      manager.setURLModifier(resource => {
        if (resource.startsWith('blob:') || resource.startsWith('data:')) return resource;
        const parsed = new URL(resource, location.href);
        if (parsed.origin !== location.origin || !parsed.pathname.startsWith('/api/')) throw new Error('The model references a resource outside the local workspace.');
        return parsed.href;
      });
      const gltf = await new GLTFLoader(manager).loadAsync(url, progress => {
        if (id !== this.loadId) return;
        this.callbacks.onProgress(progress.total ? `Loading model · ${Math.round(progress.loaded / progress.total * 100)}%` : `Loading model · ${(progress.loaded / 1048576).toFixed(1)} MB received`);
      });
      if (resourceFailed) { this.disposeObject(gltf.scene); throw new Error('An embedded model texture could not load. Check the local server’s resource policy and refresh the preview.'); }
      if (id !== this.loadId) { this.disposeObject(gltf.scene); return; }
      if (this.mixer) this.mixer.stopAllAction();
      if (this.model) { this.scene.remove(this.model); this.disposeObject(this.model); }
      if (this.helper) { this.scene.remove(this.helper); this.helper.dispose(); }
      this.model = gltf.scene; this.scene.add(this.model);
      let meshes = 0, bones = 0;
      this.model.traverse(node => {
        if (node.isMesh) { meshes++; node.frustumCulled = false; }
        if (node.isBone) bones++;
      });
      this.helper = new THREE.SkeletonHelper(this.model); this.helper.visible = this.showSkeleton;
      this.helper.material.depthTest = false; this.helper.renderOrder = 5; this.scene.add(this.helper);
      this.mixer = null; this.animationAction = null; this.duration = 0; this.time = 0;
      const clip = gltf.animations.find(item => item.duration > 0);
      if (clip) {
        this.mixer = new THREE.AnimationMixer(this.model);
        const action = this.mixer.clipAction(clip);
        this.animationAction = action;
        action.setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true; action.play();
        this.duration = clip.duration; this.mixer.setTime(0);
      }
      this.setWireframe(this.wireframe); this.fit();
      this.callbacks.onLoaded({ meshes, bones, duration: this.duration, animations: gltf.animations.length });
      this.callbacks.onTime(0, this.duration, false);
    } catch (error) { if (id === this.loadId) this.callbacks.onError(error); }
  }

  fit() {
    if (!this.model) return;
    this.model.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.model, true);
    if (box.isEmpty()) return;
    const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z, 0.01);
    const distance = span / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * Math.max(1, 1 / this.camera.aspect) * 1.35;
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(new THREE.Vector3(0.22, 0.08, 1).normalize().multiplyScalar(distance));
    this.camera.near = span / 1000; this.camera.far = span * 1000;
    this.controls.minDistance = span * 0.15; this.controls.maxDistance = span * 25;
    this.camera.updateProjectionMatrix();
    this.grid.position.set(center.x, box.min.y - span * 0.002, center.z);
    this.grid.scale.setScalar(span / 2);
    this.controls.update();
  }

  seek(value) {
    this.time = Math.min(Math.max(Number(value), 0), this.duration); this.playing = false;
    if (this.mixer) {
      // A clamped action pauses at the last frame; reset its state before an
      // explicit seek so returning to earlier frames remains deterministic.
      this.animationAction.paused = false;
      this.mixer.setTime(this.time);
    }
    this.callbacks.onTime(this.time, this.duration, this.playing);
  }

  togglePlay() {
    if (!this.mixer) return;
    if (this.time >= this.duration) this.seek(0);
    this.playing = !this.playing;
    this.callbacks.onTime(this.time, this.duration, this.playing);
  }

  setWireframe(value) {
    this.wireframe = value;
    this.model?.traverse(node => { for (const material of [node.material].flat().filter(Boolean)) material.wireframe = value; });
  }
  setSkeleton(value) { this.showSkeleton = value; if (this.helper) this.helper.visible = value; }
  clear() {
    this.loadId++; this.playing = false; this.time = 0; this.duration = 0;
    this.mixer?.stopAllAction(); this.mixer = null; this.animationAction = null;
    if (this.model) { this.scene.remove(this.model); this.disposeObject(this.model); this.model = null; }
    if (this.helper) { this.scene.remove(this.helper); this.helper.dispose(); this.helper = null; }
    this.callbacks.onTime(0, 0, false);
  }
}
