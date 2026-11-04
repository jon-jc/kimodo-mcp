export function moduleLoadMessage(message) {
  if (!/failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|unable to preload css|loading (?:css )?chunk .+ failed|failed to load module script/i.test(String(message))) return message;
  return 'Studio’s interface files may have changed since this page opened. Your current draft is still here. Save or copy it, then refresh the page and reopen this tool. If it still fails, check that the local Studio server is running.';
}

export async function loadStudioModule(loader) {
  try { return await loader(); }
  catch (error) {
    const message = moduleLoadMessage(error?.message);
    if (message === error?.message) throw error;
    throw new Error(message, { cause: error });
  }
}
