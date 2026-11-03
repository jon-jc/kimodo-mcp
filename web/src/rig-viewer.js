import * as THREE from 'three';
import { MotionViewer } from './viewer.js';
import { beforeFrame, bodyGroup, jointChain, sourceBounds, visibleJoints } from './rig-data.js';

const COLORS = { torso: 0xd9d2f0, head: 0xf0d8aa, 'left-arm': 0x71c7d9, 'right-arm': 0xe4a6bd, 'left-hand': 0x80e3bf, 'right-hand': 0xf1c58a, 'left-leg': 0x87aee5, 'right-leg': 0xc7a7ed };

// Exact source samples only. Draft controls never silently modify this display.
export class RigViewer extends MotionViewer {
  constructor(container, callbacks) {
    super(container, callbacks);
    this.onSelect = callbacks.onSelect;
    this.selected = null; this.filters = {}; this.showLabels = false; this.showGhost = true;
    this.labels = document.createElement('div'); this.labels.className = 'rig-label-layer'; container.append(this.labels);
    this.renderer.domElement.setAttribute('aria-label', 'Source skeleton. Click a joint, or use Up and Down to select joints, Left and Right to step frames, Space to play, F to fit.');
    const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();
    let down = null;
    this.renderer.domElement.addEventListener('pointerdown', event => { down = [event.clientX, event.clientY]; });
    this.renderer.domElement.addEventListener('pointerup', event => {
      if (!down || Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 5 || !this.data) return;
      const rect = this.renderer.domElement.getBoundingClientRect();
      pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
      raycaster.setFromCamera(pointer, this.camera);
      const hit = raycaster.intersectObjects(this.joints.filter(joint => joint.visible))[0];
      if (hit) this.onSelect?.(hit.object.userData.jointIndex);
    });
    this.renderer.domElement.addEventListener('keydown', event => {
      if (!this.data || event.altKey || event.ctrlKey || event.metaKey) return;
      if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', ' ', 'Home', 'End', 'f', 'F', 'Escape'].includes(event.key)) event.preventDefault();
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') this.seek(this.time + (event.key === 'ArrowLeft' ? -1 : 1) / this.data.fps);
      else if (event.key === ' ') this.togglePlay();
      else if (event.key === 'Home' || event.key === 'End') this.seek(event.key === 'Home' ? 0 : this.duration);
      else if (event.key.toLowerCase() === 'f') this.fit();
      else if (event.key === 'Escape') this.onSelect?.(null);
      else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
        const visible = this.joints.map((joint, index) => joint.visible ? index : -1).filter(index => index >= 0);
        if (!visible.length) return;
        const offset = event.key === 'ArrowDown' ? 1 : -1, current = visible.indexOf(this.selected);
        this.onSelect?.(visible[current < 0 ? (offset > 0 ? 0 : visible.length - 1) : (current + offset + visible.length) % visible.length]);
      }
    });
    this.onFrame = delta => {
      if (!this.data) return;
      if (this.playing) {
        this.time = Math.min(this.time + delta * this.speed, this.duration);
        if (this.time >= this.duration) this.playing = false;
        this.updatePose(); this.callbacks.onTime(this.time, this.duration, this.playing);
      }
      this.updateLabels();
    };
  }

  loadMotion(data, original = null) {
    this.clear(); this.data = data; this.original = original;
    if (original) beforeFrame(data, original, 0);
    const current = sourceBounds(data), compared = original ? sourceBounds(data, original) : current;
    this.bounds = new THREE.Box3(new THREE.Vector3(...current.min), new THREE.Vector3(...current.max));
    const span = Math.max(...this.bounds.getSize(new THREE.Vector3()).toArray(), 0.1);
    this.currentBounds = this.bounds.clone(); this.comparisonBounds = new THREE.Box3(new THREE.Vector3(...compared.min), new THREE.Vector3(...compared.max));
    this.bounds = this.showGhost && data.variant_id ? this.comparisonBounds : this.currentBounds;
    this.model = new THREE.Group(); this.scene.add(this.model); this.joints = []; this.labelNodes = [];
    const geometry = new THREE.SphereGeometry(span * 0.007, 10, 7);
    data.joint_names.forEach((name, index) => {
      const joint = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color: COLORS[bodyGroup(name)] }));
      joint.userData.jointIndex = index; joint.renderOrder = 3; this.model.add(joint); this.joints.push(joint);
      const label = document.createElement('span'); label.className = 'rig-joint-label'; label.textContent = name; label.hidden = true; this.labels.append(label); this.labelNodes.push(label);
    });
    const segments = data.joint_parents.filter(parent => parent !== -1).length;
    const lineGeometry = new THREE.BufferGeometry();
    lineGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segments * 6), 3));
    lineGeometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(segments * 6), 3));
    this.lines = new THREE.LineSegments(lineGeometry, new THREE.LineBasicMaterial({ vertexColors: true }));
    this.model.add(this.lines);
    const ghostGeometry = new THREE.BufferGeometry();
    ghostGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segments * 6), 3));
    this.ghost = new THREE.LineSegments(ghostGeometry, new THREE.LineBasicMaterial({ color: 0x85909f, transparent: true, opacity: 0.32, depthTest: false }));
    this.model.add(this.ghost);
    this.duration = (data.frames - 1) / data.fps; this.time = 0; this.selected = null;
    this.updatePose(); this.fit();
    this.callbacks.onLoaded({ bones: data.joint_names.length, frames: data.frames, fps: data.fps, duration: this.duration, animations: 1, sourceRig: true });
    this.callbacks.onTime(0, this.duration, false);
  }

  updatePose() {
    if (!this.data) return;
    const frame = Math.max(0, Math.min(this.data.frames - 1, Math.round(this.time * this.data.fps)));
    const poses = this.data.positions[frame], before = this.original ? beforeFrame(this.data, this.original, frame) : poses;
    const visible = visibleJoints(this.data, { ...this.filters, selected: this.selected });
    const chain = jointChain(this.data.joint_parents, this.selected);
    let segment = 0;
    this.joints.forEach((joint, index) => {
      joint.position.fromArray(poses[index]); joint.visible = visible[index];
      joint.scale.setScalar(index === this.selected ? 1.9 : chain.has(index) ? 1.2 : 1);
      joint.material.color.set(index === this.selected ? 0xffe191 : chain.has(index) ? 0xd1bcff : COLORS[bodyGroup(this.data.joint_names[index])]);
      const parent = this.data.joint_parents[index];
      if (parent < 0) return;
      const show = visible[index] && visible[parent];
      const first = poses[parent], second = show ? poses[index] : first;
      this.lines.geometry.attributes.position.array.set([...first, ...second], segment * 6);
      this.ghost.geometry.attributes.position.array.set([...before[parent], ...(show ? before[index] : before[parent])], segment * 6);
      const color = new THREE.Color(chain.has(index) && chain.has(parent) ? 0xd2bcff : COLORS[bodyGroup(this.data.joint_names[index])]);
      this.lines.geometry.attributes.color.array.set([...color.toArray(), ...color.toArray()], segment * 6); segment++;
    });
    this.lines.geometry.attributes.position.needsUpdate = true;
    this.lines.geometry.attributes.color.needsUpdate = true;
    this.ghost.geometry.attributes.position.needsUpdate = true;
    this.lines.geometry.computeBoundingSphere(); this.ghost.geometry.computeBoundingSphere();
    this.ghost.visible = this.showGhost && Boolean(this.original && this.data.variant_id);
    this.updateLabels();
  }

  updateLabels() {
    if (!this.data) return;
    this.joints.forEach((joint, index) => {
      const label = this.labelNodes[index], point = joint.position.clone().project(this.camera);
      label.hidden = !joint.visible || (!this.showLabels && index !== this.selected) || point.z < -1 || point.z > 1 || Math.abs(point.x) > 1 || Math.abs(point.y) > 1;
      if (!label.hidden) { label.style.left = `${(point.x + 1) / 2 * this.container.clientWidth}px`; label.style.top = `${(1 - point.y) / 2 * this.container.clientHeight}px`; label.classList.toggle('selected', index === this.selected); }
    });
  }

  select(index) { this.selected = index; this.updatePose(); }
  setFilters(filters) { this.filters = filters; this.updatePose(); }
  setLabels(value) { this.showLabels = value; this.updateLabels(); }
  setGhost(value) { this.showGhost = value; if (this.data) this.bounds = value && this.data.variant_id ? this.comparisonBounds : this.currentBounds; this.updatePose(); this.fit(); }
  seek(value) { if (!this.data) return; this.time = Math.max(0, Math.min(Number(value), this.duration)); this.playing = false; this.updatePose(); this.callbacks.onTime(this.time, this.duration, false); }
  togglePlay() { if (!this.data || !this.duration) return; if (this.time >= this.duration) this.seek(0); this.playing = !this.playing; this.callbacks.onTime(this.time, this.duration, this.playing); }
  fit(direction = 'perspective') {
    if (!this.bounds) return;
    const center = this.bounds.getCenter(new THREE.Vector3()), size = this.bounds.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z, 0.1);
    const distance = span / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * Math.max(1, 1 / this.camera.aspect) * 1.35;
    const offset = { front: [0, 0, 1], side: [1, 0, 0], top: [0, 1, 0.001], perspective: [0.25, 0.08, 1] }[direction] ?? [0.25, 0.08, 1];
    this.camera.position.copy(center).add(new THREE.Vector3(...offset).normalize().multiplyScalar(distance));
    this.camera.near = span / 1000; this.camera.far = span * 1000; this.camera.updateProjectionMatrix();
    this.controls.target.copy(center); this.controls.minDistance = span * 0.05; this.controls.maxDistance = span * 25; this.controls.update();
    this.grid.position.set(center.x, this.bounds.min.y - span * 0.002, center.z); this.grid.scale.setScalar(span / 2);
  }
  clear() {
    this.data = null; this.original = null; this.bounds = null;
    this.labels?.replaceChildren(); this.joints = []; this.labelNodes = [];
    super.clear();
  }
}
