def apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0):
    # Apply constraints to adjust the scores and confidence levels
    if abs(score_gate - 0.8) > 4:
        score_gate = min(max(0.8 - 4, 0), 1)
    
    if abs(conf_gate - 0.9) > 0.08:
        conf_gate = min(max(0.9 - 0.08, 0.82), 0.92)

    return conf_gate, score_gate

# Example usage
conf_gate, score_gate = apply_overrides(0.7, 0.8)
print(f"Adjusted Confidence Gate: {conf_gate}")
print(f"Adjusted Score Gate: {score_gate}")