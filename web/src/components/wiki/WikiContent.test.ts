import { describe, expect, it } from 'vitest'

import { extractTocFromMarkdown, remarkTaskStatus, stripLegacyQuizWrapper } from './WikiContent'

describe('extractTocFromMarkdown quiz terminology', () => {
  it('displays a legacy Checkpoint heading as Quiz without changing its anchor', () => {
    expect(extractTocFromMarkdown('## Checkpoint\n\n### Review')).toEqual([
      { id: 'checkpoint', text: 'Quiz', level: 2 },
      { id: 'review', text: 'Review', level: 3 },
    ])
  })

  it('removes the redundant generated wrapper directly around a quiz', () => {
    const content = `## Checkpoint

Try to answer without looking back.

\`\`\`quiz
questions:
  - prompt: Question?
    options: [One, Two]
    answer: 0
\`\`\`
`
    const normalized = stripLegacyQuizWrapper(content)

    expect(normalized).not.toContain('Checkpoint')
    expect(normalized).not.toContain('Try to answer without looking back.')
    expect(normalized).toContain('```quiz')
    expect(extractTocFromMarkdown(normalized)).toEqual([])
  })
})

interface TestNode {
  type: string
  value?: string
  checked?: boolean | null
  children?: TestNode[]
  data?: { hProperties?: Record<string, unknown> }
}

function taskItem(text: string, checked: boolean | null = null): TestNode {
  return {
    type: 'listItem',
    checked,
    children: [{ type: 'paragraph', children: [{ type: 'text', value: text }] }],
  }
}

function taskStatusOf(node: TestNode): unknown {
  return node.data?.hProperties?.['data-task']
}

describe('remarkTaskStatus', () => {
  const transform = remarkTaskStatus()

  it('tags gfm-parsed checkboxes as done and todo', () => {
    const done = taskItem('shipped', true)
    const todo = taskItem('pending', false)
    const root: TestNode = { type: 'root', children: [{ type: 'list', children: [done, todo] }] }

    transform(root)

    expect(taskStatusOf(done)).toBe('done')
    expect(taskStatusOf(todo)).toBe('todo')
  })

  it('tags [~] as in_progress and strips the marker', () => {
    const item = taskItem('[~] Ship it')

    transform(item)

    expect(taskStatusOf(item)).toBe('in_progress')
    expect(item.children?.[0].children?.[0].value).toBe('Ship it')
  })

  it('tags [!] as blocked, including nested list items', () => {
    const nested = taskItem('[!] Blocked on API')
    const parent = taskItem('[~] Parent task')
    parent.children!.push({ type: 'list', children: [nested] })

    transform({ type: 'root', children: [{ type: 'list', children: [parent] }] })

    expect(taskStatusOf(parent)).toBe('in_progress')
    expect(taskStatusOf(nested)).toBe('blocked')
    expect(nested.children?.[0].children?.[0].value).toBe('Blocked on API')
  })

  it('leaves markerless items and [!]-without-space text untouched', () => {
    const plain = taskItem('just a bullet')
    const callout = taskItem('[!]Note this is not a task')

    transform({ type: 'root', children: [{ type: 'list', children: [plain, callout] }] })

    expect(taskStatusOf(plain)).toBeUndefined()
    expect(taskStatusOf(callout)).toBeUndefined()
    expect(callout.children?.[0].children?.[0].value).toBe('[!]Note this is not a task')
  })
})
