/** Serialize every key/value pair so secret-leak tests inspect the whole store. */
export function storageSnapshot(storage: Storage): string {
  const entries: Array<[string, string | null]> = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null) entries.push([key, storage.getItem(key)]);
  }
  return JSON.stringify(entries);
}
