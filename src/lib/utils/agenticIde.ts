export type IdeFile = {
	id: string;
	path: string;
	content: string;
	language: string;
};

export type IdeWorkspace = {
	version: 1;
	files: IdeFile[];
	openFileIds: string[];
	activeFileId: string;
	stdin: string;
};

const LANGUAGE_BY_EXTENSION: Record<string, string> = {
	py: 'python',
	js: 'javascript',
	mjs: 'javascript',
	cjs: 'javascript',
	ts: 'typescript',
	sh: 'bash',
	bash: 'bash',
	c: 'c',
	cc: 'cpp',
	cpp: 'cpp',
	cxx: 'cpp',
	java: 'java',
	go: 'go',
	rs: 'rust',
	json: 'json',
	md: 'markdown',
	html: 'html',
	css: 'css',
	svelte: 'svelte',
	txt: 'text'
};

const EXTENSION_BY_LANGUAGE: Record<string, string> = {
	python: 'py',
	javascript: 'js',
	typescript: 'ts',
	bash: 'sh',
	c: 'c',
	cpp: 'cpp',
	java: 'java',
	go: 'go',
	rust: 'rs'
};

export const SANDBOX_LANGUAGES = new Set([
	'python',
	'javascript',
	'typescript',
	'bash',
	'c',
	'cpp',
	'java',
	'go',
	'rust'
]);

const makeId = () =>
	typeof crypto !== 'undefined' && crypto.randomUUID
		? crypto.randomUUID()
		: `file-${Date.now()}-${Math.random().toString(16).slice(2)}`;

export const normalizeIdePath = (value: string): string => {
	const parts = value
		.replace(/\\/g, '/')
		.split('/')
		.map((part) => part.trim())
		.filter((part) => part && part !== '.' && part !== '..');
	return parts.join('/').slice(0, 240);
};

export const languageForPath = (path: string): string => {
	const extension = path.split('.').at(-1)?.toLowerCase() ?? '';
	return LANGUAGE_BY_EXTENSION[extension] ?? 'text';
};

export const runnerLanguageForPath = (path: string): string | null => {
	const language = languageForPath(path);
	return SANDBOX_LANGUAGES.has(language) ? language : null;
};

export const pathForLanguage = (language: string, prefix = 'agent-run'): string =>
	`${prefix}.${EXTENSION_BY_LANGUAGE[language.toLowerCase()] ?? 'txt'}`;

export const createIdeFile = (path: string, content = ''): IdeFile => {
	const normalizedPath = normalizeIdePath(path);
	if (!normalizedPath) throw new Error('File path is required');
	return {
		id: makeId(),
		path: normalizedPath,
		content,
		language: languageForPath(normalizedPath)
	};
};

export const createDefaultWorkspace = (): IdeWorkspace => {
	const main = createIdeFile(
		'main.py',
		`def greet(name: str) -> str:\n    return f"Hello, {name}!"\n\n\nif __name__ == "__main__":\n    print(greet("AVEXIE"))\n`
	);
	const readme = createIdeFile(
		'README.md',
		'# Agentic workspace\n\nEdit a runnable file, press Run, or ask the chat agent to work on the active file.\n'
	);
	return {
		version: 1,
		files: [main, readme],
		openFileIds: [main.id],
		activeFileId: main.id,
		stdin: ''
	};
};

export const parseWorkspace = (value: string | null): IdeWorkspace | null => {
	if (!value) return null;
	try {
		const parsed = JSON.parse(value) as Partial<IdeWorkspace>;
		if (parsed.version !== 1 || !Array.isArray(parsed.files) || parsed.files.length === 0) {
			return null;
		}
		const files = parsed.files
			.filter(
				(file): file is IdeFile =>
					typeof file?.id === 'string' &&
					typeof file?.path === 'string' &&
					typeof file?.content === 'string'
			)
			.map((file) => ({
				...file,
				path: normalizeIdePath(file.path),
				language: languageForPath(file.path)
			}))
			.filter((file) => file.path);
		if (files.length === 0) return null;
		const ids = new Set(files.map((file) => file.id));
		const openFileIds = (parsed.openFileIds ?? []).filter((id) => ids.has(id));
		const activeFileId = ids.has(parsed.activeFileId ?? '')
			? (parsed.activeFileId as string)
			: files[0].id;
		return {
			version: 1,
			files,
			openFileIds: openFileIds.length ? openFileIds : [activeFileId],
			activeFileId,
			stdin: typeof parsed.stdin === 'string' ? parsed.stdin : ''
		};
	} catch {
		return null;
	}
};
