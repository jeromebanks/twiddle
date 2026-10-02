# Diagnosis template

Body file for `transition ... --kind diagnosis`. The tool adds the marker and header.

```markdown
## What is wrong
One paragraph, in the poster's terms and then in the code's.

## Reproduction
Exact read-only steps and what they showed - or "Not reproduced: <why>" and what was checked instead.

## Evidence
`path/file.py:123` and what it does; logs; measurements. Candidate causes ranked if unreproduced.

## Proposed fix
Tasks, in order, each with its dependency:
1. ...

## Verification
How we will know it is fixed (tests, a read-only command, what the poster should try).

## Safety class
Does fixing or verifying it write to a speaker?
```
