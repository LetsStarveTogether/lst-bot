from asyncio import CancelledError, Future, wait


async def await_cleanup[T](task: Future[T]) -> None:
    cancelled: CancelledError | None = None
    while not task.done():
        try:
            await wait((task,))
        except CancelledError as exc:
            cancelled = exc
    try:
        await task
    except CancelledError as cleanup_error:
        if cancelled is None:
            raise
        raise cancelled from cleanup_error
    except BaseException as cleanup_error:
        if cancelled is None:
            raise
        msg = "Cleanup failed after cancellation"
        raise BaseExceptionGroup(msg, [cancelled, cleanup_error]) from None
    if cancelled is not None:
        raise cancelled
