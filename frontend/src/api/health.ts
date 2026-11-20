export interface Health {
  status: string;
  version: string;
  environment: string;
}

/** GET /api/health. Throws when the backend is unreachable or answers with an error. */
export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch("/api/health", { signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as Health;
}
