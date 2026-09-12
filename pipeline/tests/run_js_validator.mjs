// Tiny Node.js shim used by test_plan_cross_validation.py.
// Reads a JSON array of production-plan documents from stdin and writes a
// JSON array of per-document error-string arrays to stdout.
import { readFileSync } from 'node:fs';
import { validateProductionPlan } from '../../bot/src/plan.js';

const stdinBuf = readFileSync(0, 'utf-8');
const documents = JSON.parse(stdinBuf);
if (!Array.isArray(documents)) {
  process.stderr.write('shim expected a JSON array of documents\n');
  process.exit(2);
}

const results = documents.map((doc) => validateProductionPlan(doc));
process.stdout.write(JSON.stringify(results));
