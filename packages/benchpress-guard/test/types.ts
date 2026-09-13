/**
 * Type test, checked by `tsc -p tsconfig.json` (part of `npm test`): guarded tools stay assignable wherever the
 * AI SDK expects tools, keep their keys and keep each tool's typed `execute`.
 */
import { generateText, streamText, tool, type ToolSet } from "ai";
import { z } from "zod";
import { guardTools } from "../src/index.js";

const tools = {
  getWeather: tool({
    description: "Weather for a city",
    inputSchema: z.object({ city: z.string() }),
    execute: async ({ city }) => ({ city, tempC: 21 }),
  }),
  cancelOrder: tool({
    description: "Cancel an order",
    inputSchema: z.object({ orderId: z.string() }),
    execute: async ({ orderId }) => ({ cancelled: orderId }),
  }),
  confirm: tool({ description: "Client-side confirmation", inputSchema: z.object({ question: z.string() }) }),
};

const guarded = guardTools(tools, { rules: [{ tool: "cancelOrder", allow_destructive: true, max_calls: 1 }] });

export const asToolSet: ToolSet = guarded;
export const sameShape: typeof tools = guarded;
export const executeKept: typeof tools.getWeather.execute = guarded.getWeather.execute;

type GenerateTextTools = NonNullable<Parameters<typeof generateText<typeof guarded>>[0]["tools"]>;
type StreamTextTools = NonNullable<Parameters<typeof streamText<typeof guarded>>[0]["tools"]>;
export const forGenerateText: GenerateTextTools = guarded;
export const forStreamText: StreamTextTools = guarded;

// @ts-expect-error keys are preserved exactly, so a missing tool is a type error
guarded.deleteEverything;
