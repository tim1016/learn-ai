namespace Backend.Services.Interfaces;

public interface IParityVerdictService
{
    /// <summary>
    /// Compute and freeze the parity verdict for a group once its LEAN
    /// companion run has been persisted. Conditional: only a
    /// <c>pending</c> verdict row is transitioned; terminal rows are
    /// never overwritten (first terminal state wins).
    /// </summary>
    Task ComputeForLeanRunAsync(int rightExecutionId, string parityGroupId, CancellationToken ct);
}
