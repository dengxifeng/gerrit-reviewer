export function transformGerritReview(ctx) {
    return {
        message: ctx.payload.message,
        agentId: ctx.payload.agentId,
        sessionKey: ctx.payload.sessionKey,
        wakeMode: ctx.payload.wakeMode,
        deliver: ctx.payload.deliver ?? true,
        channel: ctx.payload.channel ?? "last",
        to: ctx.payload.to
    }
}
