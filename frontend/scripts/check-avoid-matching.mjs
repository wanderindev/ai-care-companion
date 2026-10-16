// EVITAR name-match check of the frontend mirror (`src/lib/meds.js`) against
// the table the backend tests use (`backend/tests/avoid_match_cases.json`),
// so both matchers stay identical. Run with `npm run check:meds`.
import { readFileSync } from 'node:fs'
import { avoidMatches } from '../src/lib/meds.js'

const cases = JSON.parse(
  readFileSync(new URL('../../backend/tests/avoid_match_cases.json', import.meta.url), 'utf8'),
)

const failures = []
for (const [expected, list] of [
  [true, cases.match],
  [false, cases.no_match],
]) {
  for (const [substance, name] of list) {
    const got = avoidMatches({ name }, [{ substance }]).length > 0
    if (got !== expected)
      failures.push(`${expected ? 'missed' : 'false match'}: «${substance}» vs «${name}»`)
  }
}

const total = cases.match.length + cases.no_match.length
if (failures.length) {
  console.error(failures.join('\n'))
  console.error(`${failures.length} of ${total} cases failed`)
  process.exit(1)
}
console.log(`${total} EVITAR match cases OK`)
