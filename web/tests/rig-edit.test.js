import test from 'node:test';
import assert from 'node:assert/strict';
import { draftPose, editKey, jointWeight, manipulateDraft, pixelJointPick, validateDraftPreview, windowAroundFrame } from '../src/rig-edit.js';
import { DraftHistory, freshEdit } from '../src/rig-data.js';

const I = [[1,0,0],[0,1,0],[0,0,1]];
const near = (actual, expected, epsilon = 1e-8) => assert.ok(Math.abs(actual - expected) < epsilon, `${actual} != ${expected}`);
function fixture(frame = 10) {
  const edit = freshEdit(30);
  edit.joint_adjustments = [{ joint_name: 'Arm', local_xyz_radians: [0,0,0], start_frame: 0, end_frame_exclusive: 30, blend_frames: 3 }];
  const pose = { original_frame: frame, original_positions_m: [[0,1,0],[1,1,0],[2,1,0]], original_global_rot_mats: [I,I,I], positions_m: [[0,1,0],[1,1,0],[2,1,0]], global_rot_mats: [I,I,I] };
  return { job_id:'a'.repeat(32), sample_index:0, original_source_sha256:'b'.repeat(64), edit,
    joint_names:['Root','Arm','Hand'], joint_parents:[-1,0,1], parent_local_offsets:[[0,0,0],[1,0,0],[1,0,0]],
    first_retained_root_position:[0,1,0], frames_preview:[pose] };
}

test('local rotation changes descendants and preserves parent position and bone length', () => {
  const bundle = fixture(), original = structuredClone(bundle), edit = structuredClone(bundle.edit);
  edit.joint_adjustments[0].local_xyz_radians[2] = Math.PI / 2;
  const pose = draftPose(bundle, edit);
  assert.deepEqual(pose.positions_m[1], [1,1,0]); near(pose.positions_m[2][0],1); near(pose.positions_m[2][1],2);
  assert.deepEqual(bundle, original);
});

test('smoothstep weights use original indices and zero-blend edge poses remain editable', () => {
  const adjustment = fixture().edit.joint_adjustments[0];
  assert.equal(jointWeight(adjustment,0),0); near(jointWeight(adjustment,1),7/27);
  assert.equal(jointWeight(adjustment,3),1); assert.equal(jointWeight(adjustment,29),0);
  assert.deepEqual(windowAroundFrame(0, freshEdit(120)), {start_frame:0,end_frame_exclusive:120,blend_frames:0});
  assert.deepEqual(windowAroundFrame(60, freshEdit(120)), {start_frame:45,end_frame_exclusive:76,blend_frames:3});
  assert.deepEqual(windowAroundFrame(119, freshEdit(120)), {start_frame:0,end_frame_exclusive:120,blend_frames:0});
  assert.equal(jointWeight(windowAroundFrame(0, freshEdit(120)), 16), 1);
  assert.deepEqual(windowAroundFrame(10, { ...freshEdit(120), start_frame: 10, end_frame_exclusive: 90 }), { start_frame: 10, end_frame_exclusive: 90, blend_frames: 0 });
});

test('moving a hand aims its parent instead of translating or stretching the skeleton', () => {
  const bundle = fixture();
  const edit = manipulateDraft(bundle,bundle.edit,{mode:'move',selected:2,position:[1,4,0]});
  near(edit.joint_adjustments[0].local_xyz_radians[2], Math.PI/2);
  const pose = draftPose(bundle,edit); near(pose.positions_m[2][1],2);
  assert.deepEqual(pose.positions_m[0],[0,1,0]); assert.deepEqual(pose.positions_m[1],[1,1,0]);
});

test('root movement stays planar and retains current placement, including rotated world origin', () => {
  const bundle=fixture();const edit=structuredClone(bundle.edit);edit.yaw_radians=Math.PI/2;edit.translation_xz=[4,3];
  const changed=manipulateDraft(bundle,edit,{mode:'move',selected:0,position:[5,9,7]});
  assert.deepEqual(changed.translation_xz,[5,7]);assert.equal(draftPose(bundle,changed).positions_m[0][1],1);
  changed.root_origin='first_frame';bundle.first_retained_root_position=[2,1,0];
  const result=draftPose(bundle,changed);near(result.positions_m[0][2],9);
});

test('rotation controls map world orientation back to bounded source-local Euler offsets', () => {
  const bundle=fixture();const edit=manipulateDraft(bundle,bundle.edit,{mode:'rotate',selected:1,quaternion:[0,0,Math.sin(Math.PI/8),Math.cos(Math.PI/8)]});
  near(edit.joint_adjustments[0].local_xyz_radians[2],Math.PI/4);
  const edge=fixture(0);assert.throws(()=>manipulateDraft(edge,edge.edit,{mode:'rotate',selected:1,quaternion:[0,0,0,1]}),/blend influence/);
  assert.throws(()=>manipulateDraft(bundle,bundle.edit,{mode:'move',selected:1,position:[2,2,0]}),/attaches to the root/);
});

test('pixel picking stays usable when tiny joints are distant and ignores invisible/clipped joints', () => {
  const points=[{index:0,x:100,y:100,depth:.2,visible:true},{index:1,x:105,y:100,depth:.1,visible:false},{index:2,x:105,y:100,depth:2,visible:true}];
  assert.equal(pixelJointPick(points,110,100),0);assert.equal(pixelJointPick(points,120,100),null);
});

test('draft preview binds source identity, exact edit and original frame', () => {
  const bundle=fixture();const context={job:bundle.job_id,sample:0,sourceSha:bundle.original_source_sha256,frame:10,edit:bundle.edit,names:bundle.joint_names,parents:bundle.joint_parents};
  assert.equal(validateDraftPreview(bundle,context),bundle);
  for(const changes of [{frame:11},{sourceSha:'c'.repeat(64)},{edit:{...bundle.edit,yaw_radians:1}}])assert.throws(()=>validateDraftPreview(bundle,{...context,...changes}));
  assert.equal(editKey({b:1,a:[2,3]}),editKey({a:[2,3],b:1}));
});

test('draft revision detects changes and undo-back-to-same-content during asynchronous save', () => {
  const history=new DraftHistory(freshEdit(30));const revision=history.revision;
  history.push({...history.value,yaw_radians:.2});history.undo();
  assert.deepEqual(history.value,freshEdit(30));assert.notEqual(history.revision,revision);
});
