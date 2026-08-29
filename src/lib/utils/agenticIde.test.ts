import { describe, expect, it } from 'vitest';

import {
	createDefaultWorkspace,
	languageForPath,
	normalizeIdePath,
	parseWorkspace,
	pathForLanguage,
	runnerLanguageForPath
} from './agenticIde';

describe('agentic IDE workspace helpers', () => {
	it('normalizes workspace paths without parent traversal', () => {
		expect(normalizeIdePath('../src/../main.py')).toBe('src/main.py');
		expect(normalizeIdePath('\\src\\index.ts')).toBe('src/index.ts');
	});

	it('maps editor files to Judge0 languages', () => {
		expect(languageForPath('src/main.ts')).toBe('typescript');
		expect(runnerLanguageForPath('README.md')).toBeNull();
		expect(pathForLanguage('python')).toBe('agent-run.py');
	});

	it('round-trips a valid persisted workspace', () => {
		const workspace = createDefaultWorkspace();
		expect(parseWorkspace(JSON.stringify(workspace))).toEqual(workspace);
	});

	it('rejects invalid persisted state', () => {
		expect(parseWorkspace('{bad json')).toBeNull();
		expect(parseWorkspace(JSON.stringify({ version: 1, files: [] }))).toBeNull();
	});
});
