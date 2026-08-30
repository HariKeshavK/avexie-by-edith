<script lang="ts">
	import Badge from './Badge.svelte';
	import CodeBlock from '$lib/components/chat/Messages/CodeBlock.svelte';
	import { agenticIdeImport, showAgenticIDE, showControls } from '$lib/stores';

	export let id = '';
	export let argumentsContent = '';
	export let resultContent = '';
	export let lifecycleStatus = '';
	export let done = false;

	type RunCodeArguments = {
		language?: string;
		source?: string;
		stdin?: string;
		limits?: Record<string, unknown>;
	};

	type RunCodeResult = {
		status?: string;
		stdout?: string;
		stderr?: string;
		exit_code?: number | null;
		execution_time?: number | null;
		status_id?: number;
		status_description?: string;
		timed_out?: boolean;
		error?: string;
	};

	function parseObject(value: string): Record<string, unknown> {
		let parsed: unknown = value;
		while (typeof parsed === 'string' && parsed.trim()) {
			try {
				parsed = JSON.parse(parsed);
			} catch {
				return {};
			}
		}
		return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
			? (parsed as Record<string, unknown>)
			: {};
	}

	$: args = parseObject(argumentsContent) as RunCodeArguments;
	$: result = parseObject(resultContent) as RunCodeResult;
	$: panelStatus = done
		? 'done'
		: lifecycleStatus === 'pending' || lifecycleStatus === 'queued'
			? 'queued'
			: 'running';
	$: failed =
		Boolean(result.error) ||
		result.timed_out === true ||
		(typeof result.status_id === 'number' && result.status_id !== 3) ||
		(typeof result.exit_code === 'number' && result.exit_code !== 0);
	$: badgeType = panelStatus === 'done' ? (failed ? 'error' : 'success') : 'warning';
	$: stderr = result.error || result.stderr || '';

	const openInIDE = async () => {
		agenticIdeImport.set({
			language: args.language ?? 'text',
			source: args.source ?? '',
			stdin: args.stdin,
			result: result as Record<string, unknown>
		});
		await showControls.set(true);
		await showAgenticIDE.set(true);
	};
</script>

<div class="space-y-3" data-testid="run-code-panel">
	<div class="flex flex-wrap items-center gap-2 px-1">
		<Badge type={badgeType} content={panelStatus} />
		{#if panelStatus === 'done'}
			<span class="text-xs text-gray-500 dark:text-gray-400">
				Exit {result.exit_code ?? '—'} · {result.execution_time ?? '—'}s
				{#if result.status_description}
					· {result.status_description}{/if}
			</span>
		{:else}
			<span class="text-xs text-gray-500 dark:text-gray-400">
				{panelStatus === 'queued' ? 'Waiting for sandbox capacity' : 'Executing in Judge0'}
			</span>
		{/if}
		<button
			class="ml-auto rounded-lg border border-gray-200 px-2 py-1 text-xs text-gray-600 transition hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white"
			on:click={openInIDE}
		>
			Open in IDE
		</button>
	</div>

	<CodeBlock
		id={`${id}-source`}
		token={null}
		lang={args.language ?? ''}
		code={args.source ?? ''}
		edit={false}
		run={false}
		className="run-code-source"
	/>

	{#if args.stdin}
		<div>
			<div
				class="mb-1 px-1 text-[0.625rem] uppercase tracking-wider text-gray-400 dark:text-gray-500"
			>
				stdin
			</div>
			<pre
				class="max-h-36 overflow-auto rounded-lg bg-gray-50 p-2 text-xs whitespace-pre-wrap dark:bg-gray-900">{args.stdin}</pre>
		</div>
	{/if}

	{#if panelStatus === 'done'}
		<div class="grid gap-2 md:grid-cols-2">
			<div class="min-w-0 rounded-lg bg-gray-50 p-2 dark:bg-gray-900">
				<div class="mb-1 text-[0.625rem] uppercase tracking-wider text-gray-400 dark:text-gray-500">
					stdout
				</div>
				<pre
					class="max-h-72 overflow-auto text-xs whitespace-pre-wrap break-words font-mono">{result.stdout ||
						'—'}</pre>
			</div>
			<div class="min-w-0 rounded-lg bg-gray-50 p-2 dark:bg-gray-900">
				<div class="mb-1 text-[0.625rem] uppercase tracking-wider text-gray-400 dark:text-gray-500">
					stderr
				</div>
				<pre
					class="max-h-72 overflow-auto text-xs whitespace-pre-wrap break-words font-mono {stderr
						? 'text-red-600 dark:text-red-300'
						: ''}">{stderr || '—'}</pre>
			</div>
		</div>
	{/if}
</div>
