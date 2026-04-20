# Gold (XAUUSD) Margin Calculation Explained

This document explains why trades may fail with "Not enough money" when the account balance is low (e.g., < $100) and how the bot determines the minimum balance required.

## The Margin Formula
Standard FOREX margin calculation:

$$ \text{Margin Required} = \frac{\text{Contract Size} \times \text{Lot Size} \times \text{Price}}{\text{Leverage}} $$

### Standard Variables for Gold (XAUUSD)
- **Contract Size**: 100 ounces (standard lot)
- **Current Price**: ~$2150 - $2200 (varies)
- **Leverage**: 1:200 (common broker default)

## Example Calculation: Why 0.03 Lots Failed
If the bot attempts to open **0.03 lots** when the balance is **$60** (Assuming Price is $2200):

1.  **Notional Value**:
    $$ 100 \text{ (oz)} \times 0.03 \text{ (lots)} \times \$2200 \text{ (price)} = \$6,600 $$

2.  **Margin Required (at 1:200)**:
    $$ \frac{\$6,600}{200} = \mathbf{\$33} $$

*Note: If leverage is lower (e.g. 1:100), margin required doubles to $66.*

### Why did you see $66?
If you saw a $66 margin requirement, your leverage might be **1:100** (or the price was higher/contract size different).
$$ \frac{100 \times 0.03 \times 2200}{100} = \$66 $$

## Minimum Balance Requirement (0.01 Lots)
The absolute minimum trade is **0.01 lots**.

1.  **Notional Value (at $2200)**:
    $$ 100 \times 0.01 \times \$2200 = \$2,200 $$

2.  **Margin Required (at 1:200)**:
    $$ \frac{\$2,200}{200} = \mathbf{\$11} $$

3.  **Margin Required (at 1:500)**:
     $$ \frac{\$2,200}{500} = \mathbf{\$4.40} $$

### Can You Trade with $10?
To trade **0.01 lot** with only **$10** balance:

- **At 1:200 Leverage**: Required Margin is ~$11. **You cannot open the trade** (or it will instantly margin call).
- **At 1:500 Leverage**: Required Margin is ~$4.4. **You CAN open the trade.**

**Conclusion**:
- To trade with $10, you MUST have **1:500** leverage or higher.
- If you have 1:200, you need at least **$15-$20** safely.

## Bot Configuration Logic (`config.py`)
To prevent trade failures, the bot uses `MT5_BALANCE_TIERS` to adjust lot sizes automatically based on your balance:

| Balance Range | Lot Size | Est. Margin (1:200) | Status |
| :--- | :--- | :--- | :--- |
| **$0 - $30** | **0.01** | ~$11 | ✅ Safe |
| **$30 - $60** | **0.01** | ~$11 | ✅ Safe |
| **$60 - $100** | **0.02** | ~$22 | ✅ Safe |
| **$100 - $200** | **0.03** | ~$33 | ✅ Safe |

*Note: Margin estimates assume Gold price ~$2200.*
