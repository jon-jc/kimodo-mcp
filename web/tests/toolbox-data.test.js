import test from 'node:test';
import assert from 'node:assert/strict';
import { initialFields, controlKind, matchingTools, parseArguments, parseControl } from '../src/toolbox-data.js';

test('complete tool controls preserve omitted defaults, explicit null, false and nested data', () => {
  const schema = { properties: { flag: { type:'boolean', default:true }, nullable:{ anyOf:[{ type:'integer' }, { type:'null' }], default:null }, request:{ $ref:'#/$defs/Request' } }, required:['request'], $defs:{ Request:{ type:'object' } } };
  const values = initialFields(schema, { flag:false, nullable:null, request:{ assets:[{ id:'one' }] } });
  assert.deepEqual(values.map(item => item.included), [true,true,true]);
  assert.equal(values[0].text, 'false'); assert.equal(values[1].text, 'null');
  assert.deepEqual(parseControl('json', values[2].text, 'request'), { assets:[{ id:'one' }] });
  assert.equal(initialFields(schema)[0].included, false);
  assert.equal(controlKind(schema.properties.request, schema), 'json');
});
test('empty, nonfinite, fractional and malformed control values never become silent zeroes', () => {
  for (const value of ['', ' ', 'NaN', 'Infinity', '0.5']) assert.throws(() => parseControl('integer', value, 'attempt'));
  assert.equal(parseControl('integer', '0', 'attempt'), 0);
  assert.equal(parseControl('boolean', 'false', 'enabled'), false);
  assert.throws(() => parseControl('boolean', 'no', 'enabled'));
  assert.equal(parseControl('enum', '"skin"', 'operation'), 'skin');
  assert.equal(parseControl('string', '001', 'name'), '001');
  assert.throws(() => parseControl('json', '{', 'request'));
});
test('complete arguments require an object and preserve advanced nested controls', () => {
  for (const value of ['null','[]','true','1','{']) assert.throws(() => parseArguments(value));
  assert.deepEqual(parseArguments('{"request":{"do_sample":false,"num_beams":3}}'), { request:{ do_sample:false, num_beams:3 } });
});
test('tool search respects the live group and every search term', () => {
  const catalog = { tools:[{ name:'get_capture', description:'Read motion' }, { name:'start_capture', description:'Run motion' }], tool_groups:[{ id:'inspect', tools:['get_capture'] }] };
  assert.deepEqual(matchingTools(catalog, 'CAPTURE read', '').map(x=>x.name), ['get_capture']);
  assert.equal(matchingTools(catalog, 'motion', 'inspect').length, 1);
  assert.equal(matchingTools(catalog, '', 'missing').length, 0);
});

test('referenced primitive controls retain enum values defaults and numeric bounds', () => {
  const schema = { properties: { mode:{ $ref:'#/$defs/Mode' }, count:{ $ref:'#/$defs/Count' } },
    $defs:{ Mode:{ type:'string', enum:['skin','skeleton'], default:'skin' }, Count:{ type:'integer', minimum:1, maximum:10, default:2 } } };
  const [mode, count] = initialFields(schema);
  assert.equal(mode.kind, 'enum'); assert.deepEqual(mode.field.enum, ['skin', 'skeleton']);
  assert.equal(mode.field.default, 'skin'); assert.equal(mode.text, 'skin');
  assert.equal(count.kind, 'integer'); assert.equal(count.field.maximum, 10); assert.equal(count.text, '2');
});
