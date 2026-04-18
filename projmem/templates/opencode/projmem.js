// projmem OpenCode plugin — surfaces existing notes before edit tools run.
//
// On each `tool.execute.before`, when the tool targets a file, we shell
// out to `projmem session <file>` (JSON) and inject the note count +
// contradicted_count into the assistant's view. If contradicted_count > 0,
// the assistant sees a STOP signal and is expected to inspect via
// `projmem notes` before proceeding.

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export default {
  async 'tool.execute.before'({ tool, args }) {
    if (!['edit', 'write', 'patch'].includes(tool?.name?.toLowerCase?.())) {
      return;
    }
    const target = args?.path || args?.file || args?.target;
    if (!target) return;
    try {
      const { stdout } = await execFileAsync(
        'projmem',
        ['session', target, '--max-notes', '3', '--json'],
        { timeout: 5000 }
      );
      const data = JSON.parse(stdout);
      const notes = data.notes || {};
      const rm = data.repo_memory || {};
      const noteCount = notes.note_count ?? 0;
      const contradicted = rm.contradicted_count ?? 0;
      if (noteCount || contradicted) {
        console.log(
          `projmem: ${noteCount} note(s) on this target, ` +
          `contradicted_count=${contradicted}. ` +
          `Run projmem session <target> before editing.`
        );
      }
    } catch (_err) {
      // Stay silent on any failure — projmem missing or empty repo
      // shouldn't block the assistant.
    }
  },
};
