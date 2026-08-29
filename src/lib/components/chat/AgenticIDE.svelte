<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import JSZip from 'jszip';

	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import Badge from '$lib/components/common/Badge.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Terminal from '$lib/components/icons/Terminal.svelte';
	import { executeSandboxCode, type RunCodeResult } from '$lib/apis/sandbox';
	import { agenticIdeImport, showAgenticIDE, showControls } from '$lib/stores';
	import {
		createDefaultWorkspace,
		createIdeFile,
		languageForPath,
		normalizeIdePath,
		parseWorkspace,
		pathForLanguage,
		runnerLanguageForPath,
		type IdeFile,
		type IdeWorkspace
	} from '$lib/utils/agenticIde';

	const i18n: any = getContext('i18n');

	export let chatId: string | null = null;
	export let submitPrompt: Function = async () => {};
	export let overlay = false;

	type Activity = {
		id: string;
		kind: 'agent' | 'run' | 'file';
		label: string;
		detail: string;
		status: 'queued' | 'running' | 'done' | 'error';
		time: number;
	};

	let workspace: IdeWorkspace = createDefaultWorkspace();
	let loadedWorkspaceKey = '';
	let activeFile: IdeFile | null = null;
	let output: RunCodeResult | null = null;
	let outputError = '';
	let runStatus: 'idle' | 'queued' | 'running' | 'done' | 'error' = 'idle';
	let bottomTab: 'console' | 'activity' | 'stdin' = 'console';
	let explorerQuery = '';
	let creatingFile = false;
	let newFilePath = '';
	let agentPrompt = '';
	let agentSubmitting = false;
	let fileInput: HTMLInputElement;
	let activity: Activity[] = [];
	let limits = { cpu_time: 2, wall_time: 5, memory_kb: 512000, max_processes: 16 };

	$: activeFile = workspace.files.find((file) => file.id === workspace.activeFileId) ?? null;
	$: openFiles = workspace.openFileIds
		.map((id) => workspace.files.find((file) => file.id === id))
		.filter((file): file is IdeFile => Boolean(file));
	$: visibleFiles = workspace.files
		.filter((file) => file.path.toLowerCase().includes(explorerQuery.trim().toLowerCase()))
		.sort((a, b) => a.path.localeCompare(b.path));
	$: activeRunnerLanguage = activeFile ? runnerLanguageForPath(activeFile.path) : null;

	const workspaceKey = () => `avexie:agentic-ide:${chatId || 'draft'}`;

	const persist = () => {
		if (typeof localStorage !== 'undefined') {
			localStorage.setItem(workspaceKey(), JSON.stringify(workspace));
		}
	};

	const commit = (next: IdeWorkspace) => {
		workspace = next;
		persist();
	};

	const addActivity = (
		kind: Activity['kind'],
		label: string,
		detail: string,
		status: Activity['status']
	) => {
		activity = [
			...activity,
			{
				id: `${Date.now()}-${Math.random()}`,
				kind,
				label,
				detail,
				status,
				time: Date.now()
			}
		].slice(-80);
	};

	const openFile = (id: string) => {
		commit({
			...workspace,
			activeFileId: id,
			openFileIds: workspace.openFileIds.includes(id)
				? workspace.openFileIds
				: [...workspace.openFileIds, id]
		});
	};

	const closeFile = (id: string) => {
		const openFileIds = workspace.openFileIds.filter((fileId) => fileId !== id);
		const nextActive =
			workspace.activeFileId === id
				? (openFileIds.at(-1) ?? workspace.files.find((file) => file.id !== id)?.id ?? '')
				: workspace.activeFileId;
		commit({ ...workspace, openFileIds, activeFileId: nextActive });
	};

	const updateActiveContent = (content: string) => {
		if (!activeFile || activeFile.content === content) return;
		commit({
			...workspace,
			files: workspace.files.map((file) =>
				file.id === activeFile?.id ? { ...file, content } : file
			)
		});
	};
	const handleEditorChange: any = updateActiveContent;

	const saveActiveFile = () => {
		persist();
		if (activeFile) addActivity('file', 'Saved file', activeFile.path, 'done');
		toast.success($i18n.t('Workspace saved'));
	};

	const addFile = (path: string, content = '') => {
		const normalized = normalizeIdePath(path);
		if (!normalized) {
			toast.error($i18n.t('Enter a valid file path'));
			return null;
		}
		const existing = workspace.files.find((file) => file.path === normalized);
		if (existing) {
			openFile(existing.id);
			return existing;
		}
		const file = createIdeFile(normalized, content);
		commit({
			...workspace,
			files: [...workspace.files, file],
			openFileIds: [...workspace.openFileIds, file.id],
			activeFileId: file.id
		});
		addActivity('file', 'Created file', file.path, 'done');
		return file;
	};

	const createFile = () => {
		if (addFile(newFilePath)) {
			newFilePath = '';
			creatingFile = false;
		}
	};

	const renameFile = (file: IdeFile) => {
		const requested = window.prompt($i18n.t('Rename file'), file.path);
		if (requested === null) return;
		const path = normalizeIdePath(requested);
		if (
			!path ||
			workspace.files.some((candidate) => candidate.id !== file.id && candidate.path === path)
		) {
			toast.error($i18n.t('That file path is not available'));
			return;
		}
		commit({
			...workspace,
			files: workspace.files.map((candidate) =>
				candidate.id === file.id
					? { ...candidate, path, language: languageForPath(path) }
					: candidate
			)
		});
		addActivity('file', 'Renamed file', `${file.path} → ${path}`, 'done');
	};

	const deleteFile = (file: IdeFile) => {
		if (workspace.files.length === 1) {
			toast.error($i18n.t('A workspace must contain at least one file'));
			return;
		}
		if (!window.confirm($i18n.t('Delete {{name}}?', { name: file.path }))) return;
		const files = workspace.files.filter((candidate) => candidate.id !== file.id);
		const openFileIds = workspace.openFileIds.filter((id) => id !== file.id);
		const activeFileId = workspace.activeFileId === file.id ? files[0].id : workspace.activeFileId;
		commit({
			...workspace,
			files,
			openFileIds: openFileIds.length ? openFileIds : [activeFileId],
			activeFileId
		});
		addActivity('file', 'Deleted file', file.path, 'done');
	};

	const importFiles = async (event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		for (const selected of Array.from(input.files ?? [])) {
			const content = await selected.text();
			const existing = workspace.files.find((file) => file.path === selected.name);
			if (existing) {
				commit({
					...workspace,
					files: workspace.files.map((file) =>
						file.id === existing.id ? { ...file, content } : file
					)
				});
				openFile(existing.id);
			} else {
				addFile(selected.name, content);
			}
		}
		input.value = '';
	};

	const exportWorkspace = async () => {
		const zip = new JSZip();
		for (const file of workspace.files) zip.file(file.path, file.content);
		const blob = await zip.generateAsync({ type: 'blob' });
		const url = URL.createObjectURL(blob);
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = `avexie-workspace-${chatId || 'draft'}.zip`;
		anchor.click();
		URL.revokeObjectURL(url);
		addActivity('file', 'Exported workspace', `${workspace.files.length} files`, 'done');
	};

	const runActiveFile = async () => {
		if (!activeFile || !activeRunnerLanguage || runStatus === 'queued' || runStatus === 'running') {
			return;
		}
		output = null;
		outputError = '';
		bottomTab = 'console';
		runStatus = 'queued';
		addActivity('run', 'Queued sandbox run', activeFile.path, 'queued');
		await new Promise((resolve) => setTimeout(resolve, 0));
		runStatus = 'running';
		addActivity('run', 'Running in Judge0', activeFile.path, 'running');
		try {
			output = await executeSandboxCode(localStorage.token, {
				language: activeRunnerLanguage,
				source: activeFile.content,
				stdin: workspace.stdin,
				limits
			});
			runStatus =
				output.status_id === 3 && output.exit_code === 0 && !output.timed_out ? 'done' : 'error';
			addActivity(
				'run',
				output.timed_out ? 'Sandbox timed out' : `Process exited ${output.exit_code ?? '—'}`,
				`${activeFile.path} · ${output.execution_time ?? '—'}s`,
				runStatus === 'done' ? 'done' : 'error'
			);
		} catch (error) {
			outputError = error instanceof Error ? error.message : String(error);
			runStatus = 'error';
			addActivity('run', 'Sandbox request failed', outputError, 'error');
		}
	};

	const askAgent = async () => {
		if (!agentPrompt.trim() || !activeFile || agentSubmitting) return;
		const request = agentPrompt.trim();
		const manifest = workspace.files.map((file) => file.path).join(', ');
		const prompt = `Act as the coding agent for my AVEXIE IDE workspace.\n\nRequest: ${request}\n\nWorkspace files: ${manifest}\nActive file: ${activeFile.path}\n\n\`\`\`${activeFile.language}\n${activeFile.content}\n\`\`\`\n\nPropose the exact code change, use run_code to validate runnable code, and clearly identify the target file. Keep all execution inside the no-egress sandbox.`;
		agentSubmitting = true;
		addActivity('agent', 'Sent task to agent', request, 'running');
		agentPrompt = '';
		try {
			await submitPrompt(prompt, []);
			addActivity('agent', 'Agent task submitted', activeFile.path, 'done');
		} catch (error) {
			addActivity(
				'agent',
				'Agent task failed',
				error instanceof Error ? error.message : String(error),
				'error'
			);
		} finally {
			agentSubmitting = false;
		}
	};

	const applyToolImport = (incoming: {
		path?: string;
		language: string;
		source: string;
		stdin?: string;
		result?: Record<string, unknown>;
	}) => {
		const path = normalizeIdePath(incoming.path ?? pathForLanguage(incoming.language));
		const existing = workspace.files.find((file) => file.path === path);
		if (existing) {
			commit({
				...workspace,
				files: workspace.files.map((file) =>
					file.id === existing.id
						? { ...file, content: incoming.source, language: incoming.language }
						: file
				)
			});
			openFile(existing.id);
		} else {
			addFile(path, incoming.source);
		}
		if (incoming.stdin !== undefined) {
			workspace = { ...workspace, stdin: incoming.stdin };
			persist();
		}
		if (incoming.result) {
			output = incoming.result as unknown as RunCodeResult;
			runStatus =
				output?.status_id === 3 && output?.exit_code === 0 && !output?.timed_out
					? 'done'
					: 'error';
			bottomTab = 'console';
		}
		addActivity('agent', 'Imported agent execution', path, 'done');
	};

	const resetWorkspace = () => {
		if (!window.confirm($i18n.t('Reset this IDE workspace?'))) return;
		commit(createDefaultWorkspace());
		output = null;
		outputError = '';
		activity = [];
	};

	onMount(() => {
		loadedWorkspaceKey = workspaceKey();
		workspace =
			parseWorkspace(localStorage.getItem(loadedWorkspaceKey)) ?? createDefaultWorkspace();
		persist();

		const unsubscribe = agenticIdeImport.subscribe((incoming) => {
			if (!incoming) return;
			applyToolImport(incoming);
			agenticIdeImport.set(null);
		});

		const shortcut = (event: KeyboardEvent) => {
			if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
				event.preventDefault();
				runActiveFile();
			}
		};
		document.addEventListener('keydown', shortcut);

		return () => {
			unsubscribe();
			document.removeEventListener('keydown', shortcut);
		};
	});

	$: if (loadedWorkspaceKey && workspaceKey() !== loadedWorkspaceKey) {
		loadedWorkspaceKey = workspaceKey();
		workspace =
			parseWorkspace(localStorage.getItem(loadedWorkspaceKey)) ?? createDefaultWorkspace();
		persist();
	}
</script>

<div
	class="relative flex h-full min-h-0 flex-col bg-white text-gray-800 dark:bg-gray-950 dark:text-gray-100"
	data-testid="agentic-ide"
>
	<header
		class="flex h-12 shrink-0 items-center justify-between border-b border-gray-100 px-3 dark:border-gray-800"
	>
		<div class="flex min-w-0 items-center gap-2">
			<div
				class="flex size-7 items-center justify-center rounded-lg bg-gray-900 text-white dark:bg-white dark:text-gray-900"
			>
				<Terminal className="size-4" />
			</div>
			<div class="min-w-0">
				<div class="truncate text-sm font-medium">Agentic IDE</div>
				<div class="flex items-center gap-1.5 text-[0.65rem] text-gray-500 dark:text-gray-400">
					<span class="size-1.5 rounded-full bg-emerald-500"></span>
					Judge0 · no egress · autosaved
				</div>
			</div>
		</div>

		<div class="flex items-center gap-1.5">
			<Tooltip content="Export workspace">
				<button
					class="flex size-7 items-center justify-center rounded-lg text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
					on:click={exportWorkspace}
					aria-label="Export workspace"
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" class="size-4"
						><path
							d="M12 3v12m0 0 4-4m-4 4-4-4M5 17v3h14v-3"
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.6"
						/></svg
					>
				</button>
			</Tooltip>
			<Tooltip content="Reset workspace">
				<button
					class="flex size-7 items-center justify-center rounded-lg text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
					on:click={resetWorkspace}
					aria-label="Reset workspace"
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" class="size-4"
						><path
							d="M4 4v6h6M20 20v-6h-6M5.5 15a7 7 0 0 0 11.8 2.3L20 14M4 10l2.7-3.3A7 7 0 0 1 18.5 9"
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.6"
						/></svg
					>
				</button>
			</Tooltip>
			<button
				class="flex size-7 items-center justify-center rounded-lg text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
				on:click={() => {
					showAgenticIDE.set(false);
					showControls.set(false);
				}}
				aria-label="Close IDE"
			>
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" class="size-4"
					><path d="m6 6 12 12M18 6 6 18" stroke-linecap="round" stroke-width="1.6" /></svg
				>
			</button>
		</div>
	</header>

	<div class="flex min-h-0 flex-1">
		<aside
			class="flex w-44 shrink-0 flex-col border-r border-gray-100 bg-gray-50/50 dark:border-gray-800 dark:bg-gray-900/40 md:w-52"
		>
			<div class="flex items-center justify-between px-2 pb-1 pt-2">
				<span class="text-[0.65rem] font-medium uppercase tracking-widest text-gray-500"
					>Explorer</span
				>
				<div class="flex">
					<button
						class="flex size-6 items-center justify-center rounded text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
						on:click={() => (creatingFile = true)}
						aria-label="New file">+</button
					>
					<button
						class="flex size-6 items-center justify-center rounded text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
						on:click={() => fileInput.click()}
						aria-label="Import files">↥</button
					>
				</div>
			</div>
			<input bind:this={fileInput} class="hidden" type="file" multiple on:change={importFiles} />
			<div class="px-2 pb-2">
				<input
					bind:value={explorerQuery}
					class="h-7 w-full rounded-md border border-gray-200 bg-white px-2 text-xs text-gray-800 outline-none transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:focus:border-gray-500"
					placeholder="Filter files"
					aria-label="Filter files"
				/>
			</div>
			{#if creatingFile}
				<form class="flex gap-1 px-2 pb-2" on:submit|preventDefault={createFile}>
					<input
						bind:value={newFilePath}
						class="h-7 w-full rounded-md border border-gray-200 bg-white px-2 text-xs text-gray-800 outline-none transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:focus:border-gray-500"
						placeholder="src/file.py"
						on:blur={() => !newFilePath && (creatingFile = false)}
					/>
					<button
						type="submit"
						class="h-7 rounded-md bg-gray-900 px-2 text-[0.65rem] font-medium text-white hover:bg-gray-700 dark:bg-white dark:text-gray-900 dark:hover:bg-gray-200"
					>
						Create
					</button>
				</form>
			{/if}
			<div class="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
				{#each visibleFiles as file (file.id)}
					<div
						class="group flex items-center gap-1 rounded-md {file.id === workspace.activeFileId
							? 'bg-gray-200/70 dark:bg-gray-800'
							: 'hover:bg-gray-200/40 dark:hover:bg-gray-800/60'}"
					>
						<button
							class="min-w-0 flex-1 truncate px-2 py-1.5 text-left text-xs"
							title={file.path}
							on:click={() => openFile(file.id)}
						>
							<span class="mr-1 text-gray-400">{file.path.includes('/') ? '⌞' : '·'}</span
							>{file.path}
						</button>
						<div class="hidden shrink-0 items-center pr-1 group-hover:flex">
							<button
								class="flex size-5 items-center justify-center rounded text-[0.65rem] text-gray-400 hover:bg-gray-300 hover:text-gray-800 dark:hover:bg-gray-700 dark:hover:text-white"
								on:click={() => renameFile(file)}
								aria-label={`Rename ${file.path}`}>✎</button
							>
							<button
								class="flex size-5 items-center justify-center rounded text-[0.65rem] text-gray-400 hover:bg-gray-300 hover:text-gray-800 dark:hover:bg-gray-700 dark:hover:text-white"
								on:click={() => deleteFile(file)}
								aria-label={`Delete ${file.path}`}>×</button
							>
						</div>
					</div>
				{/each}
			</div>
			<div class="border-t border-gray-100 p-2 text-[0.65rem] text-gray-500 dark:border-gray-800">
				{workspace.files.length} files · browser workspace
			</div>
		</aside>

		<main class="flex min-w-0 flex-1 flex-col">
			<div
				class="flex h-9 shrink-0 items-end overflow-x-auto border-b border-gray-100 bg-gray-50/30 dark:border-gray-800 dark:bg-gray-900/20"
			>
				{#each openFiles as file (file.id)}
					<div
						class="flex h-full shrink-0 items-center border-r border-gray-100 text-xs dark:border-gray-800 {file.id ===
						workspace.activeFileId
							? 'border-t-2 border-t-gray-900 bg-white dark:border-t-gray-100 dark:bg-gray-950'
							: 'text-gray-500'}"
					>
						<button class="h-full max-w-44 truncate px-3" on:click={() => openFile(file.id)}
							>{file.path.split('/').at(-1)}</button
						>
						<button
							class="mr-1 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-800 dark:hover:bg-gray-800 dark:hover:text-white"
							on:click={() => closeFile(file.id)}
							aria-label={`Close ${file.path}`}>×</button
						>
					</div>
				{/each}
			</div>

			<div class="flex min-h-0 flex-1 flex-col">
				<div
					class="flex h-10 shrink-0 items-center justify-between border-b border-gray-100 px-2 dark:border-gray-800"
				>
					<div class="min-w-0 truncate px-1 text-xs text-gray-500">
						{activeFile?.path ?? 'No file open'}
					</div>
					<div class="flex items-center gap-2">
						{#if activeRunnerLanguage}
							<span class="hidden text-[0.65rem] text-gray-400 sm:inline">⌘↵ to run</span>
							<button
								class="flex h-7 items-center gap-1.5 rounded-lg bg-gray-900 px-3 text-xs font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-white dark:text-gray-900 dark:hover:bg-gray-200"
								on:click={runActiveFile}
								disabled={runStatus === 'queued' || runStatus === 'running'}
							>
								{#if runStatus === 'queued' || runStatus === 'running'}
									<span
										class="size-3 animate-spin rounded-full border border-white/40 border-t-white dark:border-black/30 dark:border-t-black"
									></span>
								{:else}
									<span>▶</span>
								{/if}
								{runStatus === 'queued' ? 'Queued' : runStatus === 'running' ? 'Running' : 'Run'}
							</button>
						{:else}
							<Badge type="muted" content="preview only" />
						{/if}
					</div>
				</div>

				<div class="min-h-[12rem] flex-1">
					{#if activeFile}
						{#key activeFile.id}
							<CodeEditor
								id={`agentic-ide-${activeFile.id}`}
								lang={activeFile.language}
								value={activeFile.content}
								onChange={handleEditorChange}
								onSave={saveActiveFile}
								className="text-xs"
							/>
						{/key}
					{:else}
						<div class="flex h-full items-center justify-center text-sm text-gray-400">
							Open a file from the explorer
						</div>
					{/if}
				</div>

				<section
					class="flex h-48 shrink-0 flex-col border-t border-gray-100 bg-gray-50/30 dark:border-gray-800 dark:bg-gray-900/20"
				>
					<div
						class="flex h-9 shrink-0 items-center justify-between border-b border-gray-100 px-2 dark:border-gray-800"
					>
						<div class="flex h-full items-center gap-1">
							{#each ['console', 'activity', 'stdin'] as tab}
								<button
									class="h-full border-b-2 px-2 text-[0.7rem] uppercase tracking-wide {bottomTab ===
									tab
										? 'border-gray-900 text-gray-900 dark:border-white dark:text-white'
										: 'border-transparent text-gray-500'}"
									on:click={() => (bottomTab = tab as typeof bottomTab)}>{tab}</button
								>
							{/each}
						</div>
						<div class="flex items-center gap-2 text-[0.65rem] text-gray-500">
							{#if runStatus !== 'idle'}<Badge
									type={runStatus === 'done'
										? 'success'
										: runStatus === 'error'
											? 'error'
											: 'warning'}
									content={runStatus}
								/>{/if}
							<span>{limits.cpu_time}s CPU · {Math.round(limits.memory_kb / 1024)} MB</span>
						</div>
					</div>
					{#if bottomTab === 'console'}
						<div class="min-h-0 flex-1 overflow-auto p-3 font-mono text-xs" aria-live="polite">
							{#if runStatus === 'queued'}<div class="text-amber-600">
									Waiting for sandbox capacity…
								</div>
							{:else if runStatus === 'running'}<div class="text-blue-600">
									Executing inside Judge0…
								</div>
							{:else if outputError}<pre
									class="whitespace-pre-wrap text-red-600 dark:text-red-300">{outputError}</pre>
							{:else if output}
								<div class="mb-2 text-[0.65rem] text-gray-500">
									exit {output.exit_code ?? '—'} · {output.execution_time ?? '—'}s · {output.status_description}
								</div>
								{#if output.stdout}<pre
										class="whitespace-pre-wrap text-gray-800 dark:text-gray-100">{output.stdout}</pre>{/if}
								{#if output.stderr}<pre
										class="mt-2 whitespace-pre-wrap text-red-600 dark:text-red-300">{output.stderr}</pre>{/if}
								{#if !output.stdout && !output.stderr}<span class="text-gray-400"
										>Process completed with no output.</span
									>{/if}
							{:else}<span class="text-gray-400"
									>Run the active file to see output. Network access is disabled.</span
								>{/if}
						</div>
					{:else if bottomTab === 'activity'}
						<div class="min-h-0 flex-1 overflow-auto px-3 py-2">
							{#if activity.length === 0}<div class="py-4 text-center text-xs text-gray-400">
									Agent and workspace actions appear here.
								</div>{/if}
							{#each [...activity].reverse() as item (item.id)}
								<div
									class="flex gap-2 border-b border-gray-100 py-1.5 text-xs last:border-0 dark:border-gray-800"
								>
									<span
										class="mt-1 size-1.5 shrink-0 rounded-full {item.status === 'error'
											? 'bg-red-500'
											: item.status === 'done'
												? 'bg-emerald-500'
												: 'bg-amber-500'}"
									></span>
									<div class="min-w-0">
										<div>{item.label}</div>
										<div class="truncate text-[0.65rem] text-gray-500">{item.detail}</div>
									</div>
									<time class="ml-auto shrink-0 text-[0.6rem] text-gray-400"
										>{new Date(item.time).toLocaleTimeString([], {
											hour: '2-digit',
											minute: '2-digit'
										})}</time
									>
								</div>
							{/each}
						</div>
					{:else}
						<div class="min-h-0 flex-1 p-2">
							<textarea
								bind:value={workspace.stdin}
								on:input={persist}
								class="h-full w-full resize-none rounded-lg border border-gray-200 bg-white p-2 font-mono text-xs outline-none focus:border-gray-400 dark:border-gray-700 dark:bg-gray-950"
								placeholder="Standard input passed to the program"
							></textarea>
						</div>
					{/if}
				</section>
			</div>

			<form
				class="flex shrink-0 items-end gap-2 border-t border-gray-100 p-2 dark:border-gray-800"
				on:submit|preventDefault={askAgent}
			>
				<div class="min-w-0 flex-1">
					<label
						for="agentic-ide-prompt"
						class="mb-1 block text-[0.65rem] font-medium uppercase tracking-wide text-gray-500"
						>Ask the agent about {activeFile?.path ?? 'this workspace'}</label
					>
					<textarea
						id="agentic-ide-prompt"
						bind:value={agentPrompt}
						class="min-h-9 w-full resize-none rounded-md border border-gray-200 bg-white px-2 py-2 text-xs text-gray-800 outline-none transition focus:border-gray-400 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:focus:border-gray-500"
						rows="1"
						placeholder="Fix the bug, add tests, and validate it…"
						on:keydown={(event) => {
							if (event.key === 'Enter' && !event.shiftKey) {
								event.preventDefault();
								askAgent();
							}
						}}
					></textarea>
				</div>
				<button
					class="h-9 rounded-lg bg-blue-600 px-3 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
					type="submit"
					disabled={!agentPrompt.trim() || agentSubmitting || !activeFile}
					>{agentSubmitting ? 'Sending…' : 'Ask agent'}</button
				>
			</form>
		</main>
	</div>

	{#if overlay}<div class="absolute inset-0 z-20"></div>{/if}
</div>
