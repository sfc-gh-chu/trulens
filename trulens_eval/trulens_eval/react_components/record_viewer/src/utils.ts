import {
  CallJSONRaw,
  PerfJSONRaw,
  RecordJSONRaw,
  StackJSONRaw,
  StackTreeNode,
} from "./types"

/**
 * Gets the name of the calling function in the stack cell.
 *
 * TODO: Is this the best name to use for the component/stack?
 *
 * See README.md for an overview of the terminology.
 *
 * @param stackCell - StackJSONRaw Cell in the stack of a call.
 * @returns name of the calling function in the stack cell.
 */
export const getNameFromCell = (stackCell: StackJSONRaw) => {
  return stackCell.method.obj.cls.name
}

/**
 * Gets the start and end times based on the performance
 * data provided.
 *
 * @param perf - PerfJSONRaw The performance data to be analyzed
 * @returns an object containing the start and end times based on the performance
 * data provided
 */
export const getStartAndEndTimes = (perf: PerfJSONRaw) => {
  return {
    startTime: perf?.start_time ? new Date(perf.start_time) : undefined,
    endTime: perf?.end_time ? new Date(perf.end_time) : undefined,
  }
}

// let's make an assumption that the nodes are
// 1. the stack cell method obj name must match
// 2. the stack cell must be within the time
const addCallToTree = (
  tree: StackTreeNode,
  call: CallJSONRaw,
  stack: StackJSONRaw[],
  index: number
) => {
  const stackCell = stack[index]

  if (!tree.children) tree.children = []

  // otherwise, we are deciding which node to go in
  let matchingNode = tree.children.find(
    (node) =>
      node.name === getNameFromCell(stackCell) &&
      (node.startTime ?? 0) <= new Date(call.perf.start_time) &&
      (node.endTime ?? Infinity) >= new Date(call.perf.end_time)
  )

  // if we are currently at the top most cell of the stack
  if (index === stack.length - 1) {
    const { startTime, endTime } = getStartAndEndTimes(call.perf)

    if (matchingNode) {
      matchingNode.startTime = startTime
      matchingNode.endTime = endTime
      matchingNode.raw = call

      return
    }

    tree.children.push({
      children: undefined,
      name: getNameFromCell(stackCell),
      startTime,
      endTime,
      raw: call,
    })

    return
  }

  if (!matchingNode) {
    const newNode = {
      children: [],
      name: getNameFromCell(stackCell),
    }

    // otherwise create a new node
    tree.children.push(newNode)
    matchingNode = newNode
  }

  addCallToTree(matchingNode, call, stack, index + 1)
}

export const createTreeFromCalls = (recordJSON: RecordJSONRaw) => {
  const tree: StackTreeNode = {
    children: [],
    name: "App",
    startTime: new Date(recordJSON.perf.start_time),
    endTime: new Date(recordJSON.perf.end_time),
  }

  for (const call of recordJSON.calls) {
    addCallToTree(tree, call, call.stack, 0)
  }

  return tree
}
