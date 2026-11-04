// Build editable controls from the live MCP contract. The server remains the
// authority for complete JSON Schema validation and operation prerequisites.
export function resolveField(schema, root) {
  if (schema?.$ref?.startsWith('#/$defs/')) return { ...root.$defs?.[schema.$ref.slice(8)], ...schema };
  return schema ?? {};
}
export function controlKind(schema, root) {
  const field = resolveField(schema, root);
  if (Array.isArray(field.enum)) return 'enum';
  if (['string', 'number', 'integer', 'boolean'].includes(field.type)) return field.type;
  return 'json';
}
export function initialFields(schema, supplied = {}) {
  return Object.entries(schema.properties ?? {}).map(([name, definition]) => {
    const field = resolveField(definition, schema);
    const kind = controlKind(field, schema), required = (schema.required ?? []).includes(name);
    const exists = Object.hasOwn(supplied, name), hasDefault = Object.hasOwn(field, 'default');
    const value = exists ? supplied[name] : hasDefault ? field.default : undefined;
    return { name, field, kind, required, included: required || exists,
      text: value === undefined ? '' : kind === 'json' ? JSON.stringify(value, null, 2) : String(value) };
  });
}
export function parseControl(kind, text, name) {
  if (kind === 'json' || kind === 'enum') {
    try { return JSON.parse(text); } catch { throw new Error(`${name}: enter valid JSON.`); }
  }
  if (kind === 'boolean') { if (!['true', 'false'].includes(text)) throw new Error(`${name}: choose true or false.`); return text === 'true'; }
  if (kind === 'number' || kind === 'integer') {
    const value = text.trim() ? Number(text) : NaN;
    if (!Number.isFinite(value) || (kind === 'integer' && !Number.isInteger(value))) throw new Error(`${name}: enter a finite ${kind}.`);
    return value;
  }
  return text;
}
export function parseArguments(text) {
  let value;
  try { value = JSON.parse(text); } catch { throw new Error('Arguments must be valid JSON.'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Arguments must be a JSON object.');
  return value;
}
export function matchingTools(catalog, query, group) {
  const names = group ? new Set(catalog.tool_groups?.find(item => item.id === group)?.tools ?? []) : null;
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return (catalog.tools ?? []).filter(tool => (!names || names.has(tool.name)) && words.every(word => `${tool.name} ${tool.description}`.toLowerCase().includes(word)));
}
