<script lang="ts">
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { safeImageUrl } from '$lib/utils/safeImageUrl';

	export let className = 'size-8';
	// LICENSE covers this AVEXIE fallback logo.
	// Do not alter, remove, obscure, or replace it except as LICENSE permits:
	// #license.
	const FALLBACK_SRC = `${WEBUI_BASE_URL}/static/favicon.png`;

	export let src = FALLBACK_SRC;
</script>

<img
	aria-hidden="true"
	src={safeImageUrl(src)}
	class=" {className} object-cover rounded-2xl"
	alt="profile"
	draggable="false"
	on:error={(e) => {
		if (!e.currentTarget.src.endsWith(FALLBACK_SRC)) {
			e.currentTarget.src = FALLBACK_SRC;
		}
	}}
/>
