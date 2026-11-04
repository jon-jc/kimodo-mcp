import * as THREE from 'three';
import { MotionViewer } from './viewer.js';
import { landmarkBounds, landmarkDisplayPoint } from './freemocap-review-data.js';

// A read-only landmark display. Missing points remove their incident lines;
// this viewer never invents rotations, contacts, confidence, or replacement poses.
export class LandmarkViewer extends MotionViewer {
  constructor(container, callbacks) {
    super(container, { onTime: () => {}, onLoaded: () => {}, onProgress: () => {}, onError: callbacks.onError });
    this.reviewCallbacks = callbacks; this.frame = 0; this.selected = null;
    this.renderer.domElement.setAttribute('aria-label', 'Captured body landmarks. Left and Right step frames, Up and Down select landmarks, Space plays, F fits the view.');
    let pointer = null;
    this.renderer.domElement.addEventListener('pointerdown', event => { pointer = [event.clientX, event.clientY]; });
    this.renderer.domElement.addEventListener('pointerup', event => {
      if (!pointer || !this.data || Math.hypot(event.clientX - pointer[0], event.clientY - pointer[1]) > 5) { pointer = null; return; }
      pointer = null;
      const box = this.renderer.domElement.getBoundingClientRect(); let nearest = null, distance = 16;
      this.points.forEach((point, index) => {
        if (!point.visible) return;
        const p = point.position.clone().project(this.camera);
        if (p.z < -1 || p.z > 1) return;
        const d = Math.hypot((p.x + 1) * box.width / 2 - (event.clientX - box.left), (1 - p.y) * box.height / 2 - (event.clientY - box.top));
        if (d < distance) { nearest = index; distance = d; }
      });
      if (nearest !== null) this.select(nearest);
    });
    this.renderer.domElement.addEventListener('keydown', event => {
      if (!this.data || event.altKey || event.ctrlKey || event.metaKey) return;
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' ', 'f', 'F'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') this.seekFrame(this.frame + (event.key === 'ArrowLeft' ? -1 : 1));
      else if (event.key === 'Home' || event.key === 'End') this.seekFrame(event.key === 'Home' ? 0 : this.data.frames - 1);
      else if (event.key === ' ') this.togglePlay();
      else if (event.key.toLowerCase() === 'f') this.fit();
      else this.select(((this.selected ?? (event.key === 'ArrowDown' ? -1 : 0)) + (event.key === 'ArrowDown' ? 1 : -1) + this.points.length) % this.points.length);
    });
    this.onFrame = delta => {
      if (!this.data || !this.playing || !this.data.fps) return;
      this.time = Math.min(this.duration, this.time + delta * this.speed);
      this.frame = Math.min(this.data.frames - 1, Math.round(this.time * this.data.fps));
      if (this.time >= this.duration) this.playing = false;
      this.updateFrame();
    };
  }

  loadLandmarks(data) {
    this.clear(); this.data = data; this.frame = 0; this.selected = null;
    const bounds = landmarkBounds(data);
    this.bounds = bounds ? new THREE.Box3(new THREE.Vector3(...bounds.min), new THREE.Vector3(...bounds.max)) : null;
    const span = this.bounds ? Math.max(...this.bounds.getSize(new THREE.Vector3()).toArray(), 0.1) : 1;
    this.model = new THREE.Group(); this.scene.add(this.model);
    const shape = new THREE.SphereGeometry(span * 0.007, 9, 6);
    this.points = data.joint_names.map((name, index) => {
      const color = data.tracked_mask[index] ? name.startsWith('left_') ? 0x80cbd6 : name.startsWith('right_') ? 0xe0a2bd : 0xd9d2f0 : 0xe9c77b;
      const point = new THREE.Mesh(shape, new THREE.MeshBasicMaterial({ color })); point.userData.color = color;
      this.model.add(point); return point;
    });
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(data.joint_names.length * 6), 3));
    this.lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0xb5c2d4 }));
    this.model.add(this.lines); this.duration = data.fps ? (data.frames - 1) / data.fps : 0;
    this.grid.visible = Boolean(bounds); this.updateFrame(); this.fit(); this.resize();
  }

  updateFrame() {
    if (!this.data) return;
    const mask = this.data.valid_mask[this.frame], positions = this.data.positions[this.frame]; let segments = 0;
    this.points.forEach((point, index) => {
      point.visible = mask[index];
      if (mask[index]) point.position.fromArray(landmarkDisplayPoint(positions[index]));
      point.scale.setScalar(index === this.selected ? 1.9 : 1);
      point.material.color.set(index === this.selected ? 0xffe191 : point.userData.color);
      const parent = this.data.joint_parents[index];
      if (parent >= 0 && mask[index] && mask[parent]) {
        this.lines.geometry.attributes.position.array.set([...landmarkDisplayPoint(positions[parent]), ...landmarkDisplayPoint(positions[index])], segments * 6);
        segments++;
      }
    });
    this.lines.geometry.setDrawRange(0, segments * 2); this.lines.geometry.attributes.position.needsUpdate = true;
    // The buffer can retain old coordinates past drawRange; culling is not useful
    // for this small overlay and must not hide a newly displayed valid segment.
    this.lines.frustumCulled = false;
    this.reviewCallbacks.onFrame?.(this.frame, this.playing, this.selected);
  }
  seekFrame(frame) {
    if (!this.data || !Number.isFinite(Number(frame))) return;
    this.playing = false; this.frame = Math.max(0, Math.min(this.data.frames - 1, Math.round(Number(frame))));
    this.time = this.data.fps ? this.frame / this.data.fps : 0; this.updateFrame();
  }
  select(index) { this.selected = index; this.updateFrame(); this.reviewCallbacks.onSelect?.(index); }
  togglePlay() {
    if (!this.data?.fps || this.data.frames < 2) return;
    if (this.frame >= this.data.frames - 1) this.seekFrame(0);
    this.playing = !this.playing; this.updateFrame();
  }
  fit() {
    if (!this.bounds) return;
    const center = this.bounds.getCenter(new THREE.Vector3());
    const span = Math.max(...this.bounds.getSize(new THREE.Vector3()).toArray(), 0.1);
    const distance = span / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * Math.max(1, 1 / this.camera.aspect) * 1.4;
    this.camera.position.copy(center).add(new THREE.Vector3(0.25, 0.1, 1).normalize().multiplyScalar(distance));
    this.camera.near = span / 1000; this.camera.far = span * 1000; this.camera.updateProjectionMatrix();
    this.controls.target.copy(center); this.controls.minDistance = span * 0.03; this.controls.maxDistance = span * 30; this.controls.update();
    this.grid.position.set(center.x, this.bounds.min.y - span * 0.002, center.z); this.grid.scale.setScalar(span / 2);
  }
  clear() { this.data = null; this.bounds = null; this.points = []; this.selected = null; this.frame = 0; super.clear(); }
}
