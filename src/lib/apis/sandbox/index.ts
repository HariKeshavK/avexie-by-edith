import { WEBUI_API_BASE_URL } from '$lib/constants';

export type SandboxLimits = {
	cpu_time?: number;
	wall_time?: number;
	memory_kb?: number;
	max_processes?: number;
};

export type RunCodeRequest = {
	language: string;
	source: string;
	stdin?: string;
	limits?: SandboxLimits;
};

export type RunCodeResult = {
	status: string;
	stdout: string;
	stderr: string;
	exit_code: number | null;
	execution_time: number | null;
	status_id: number;
	status_description: string;
	timed_out: boolean;
	memory_kb?: number | null;
};

export const executeSandboxCode = async (
	token: string,
	payload: RunCodeRequest
): Promise<RunCodeResult> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/sandbox/run`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify(payload)
	});

	if (!response.ok) {
		const body = await response.json().catch(() => ({}));
		throw new Error(body?.detail ?? `Sandbox request failed with HTTP ${response.status}`);
	}

	return response.json();
};
