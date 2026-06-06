def apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0):
    # Apply constraints to adjust the scores and confidence gates
    if abs(score_gate - 2) > 4:
        score_gate = min(2 + 4, max(2 - 4, score_gate))
    
    if abs(conf_gate - 0.9) > 0.08:
        conf_gate = min(0.9 + 0.08, max(0.9 - 0.08, conf_gate))

    return conf_gate, score_gate

# Example usage
conf_gate, score_gate = apply_overrides(0.7, 3)
print(f"Adjusted Confidence Gate: {conf_gate}, Adjusted Score Gate: {score_gate}")