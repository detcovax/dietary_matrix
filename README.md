# Dietary Matrix

A Flask app that recreates the category x macro matrix from `Dietary_Matrix.xlsx`
and fills it in from what you log, so you can see what you're hitting, missing,
or overeating for a meal, a day, or a week.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Data is stored in `dietary_matrix.db` (SQLite) next to `app.py`.
Upgrading from the first version? Replace the files and keep your `.db`; it's migrated automatically.

## Foods: ingredients and recipes

**Ingredients** are basic foods with one category. You enter:
- macros (carbs, fat, protein in grams) for one serving, and a name for that serving ("1 slice", "100 g")
- optionally, what one serving equals: a **weight**, a **volume** and/or a **count** ("2 slices").
  Each one you fill in lets you log the food in that kind of unit.

**Recipes** are built from ingredients *and other recipes*. For each part you pick an amount and unit.
You say how many servings the batch makes, and optionally the batch's weight, volume or number of
pieces. The app calculates per-serving nutrition from the parts, recursively through nested recipes.
A recipe's calories land in each part's own matrix row, so a chili fills Meats & Beans, Vegetables
and Oil & Dairy, not just one row. If you leave the batch weight blank, the ingredients' combined
weight is used (when all of them have a weight).

A recipe can't contain itself, directly or through another recipe; the app blocks that.

## Logging amounts

The amount box accepts `2`, `0.5`, `3/4`, `1 1/2` or `1½`. Units:

| Kind | Units | Needs |
| --- | --- | --- |
| Servings | servings | nothing |
| Count | each (shown as the food's piece name, e.g. "eggs") | a count |
| Weight | g, kg, oz, lb | a weight |
| Volume | ml, L, tsp, tbsp, fl oz, cup, pint, quart | a volume |

The unit menu only offers units the chosen food supports, and the form previews the calories before you log.
Weight and volume are not converted into each other, since that depends on the food's density.

## Suggestions: what to eat next

Below the matrix, the app suggests foods from your library for whatever is left of the meal, day or week you're viewing.

- Each food is tried at everyday portions (1/2 to 3 servings, shown the natural way: "1 banana", "3/4 cup", "150 g")
  and scored by how many calories of remaining gap it fills, minus twice the calories it pushes past a target.
  Overshoot uses the same tolerance as the matrix colors, so a few stray calories don't count against a food.
- **Best single picks** are the top five foods to eat next, each with a one-tap Log button.
- **One way to round out the day** picks up to four different foods in turn, each chosen for what's still open after the
  ones before it, and shows how many cells would end up on target. "Log all" adds them to the meal you pick.
- Foods you log often win close calls. Recipes with parts that can't be converted are left out.

The weighting lives at the top of the suggestions section in `app.py` (`OVER_WEIGHT`, `SERVING_STEPS`, `MAX_SERVINGS`)
if you want suggestions to be stricter about overfilling or to allow bigger portions.

## Build from what's on hand

The **Build** page (also linked from the suggestions) works out a meal from ingredients you actually have.

- Tick what's on hand, including leftover recipes. Optionally add a limit per item ("up to 2 eggs", "up to 300 g").
- Choose Meal, Day or Week. The build fills what's left of that period.
- The app adds kitchen-sized steps (1 egg, 1/4 cup, 10 g, 1 tsp) of whichever item best fills the remaining gaps,
  then fine-tunes with removals and swaps until nothing improves the fit. It uses the same scoring as the suggestions.
- The result shows each amount, a mini matrix of where you'd end up, what's still open, why any ticked item was
  left out, and one food from your library that would close the rest.
- **Log these** adds every item to the meal you choose. **Save as a recipe** opens the recipe form pre-filled, so you
  can rename it or adjust amounts first.
- Your ticks and limits are remembered for next time.

The builder fills gaps literally, so it can suggest large amounts of low-calorie foods (for example several cups of
vegetables to fill Vegetables carbs). Add a limit if you want to cap an item.

## Pages

- **Matrix**: the grid. Switch between Meal, Day and Week and log food underneath.
  Blue is below target, green is on target, red is over.
- **Foods**: your ingredients and recipes. Recipes show a bar of which matrix rows a serving fills.
- **Build**: work out a meal from the ingredients you have on hand.
- **Plan**: daily calories, the Oil & Dairy share, the 12 matrix percentages, the meal split used
  by Meal view, and how much leeway counts as "on target".

## How targets are calculated

- Cell target = matrix % x base calories x period factor.
- Oil & Dairy allowance = `ROUND(p / (1 - p) x daily, -1)`, the same formula as the sheet.
- Period factor: Day = 1, Meal = that meal's share of the day, Week = 7 days
  (or Monday through today when viewing the current week).
- **Base calories:** the spreadsheet multiplies the matrix % by daily calories (1,900), but the
  percentages add up to 95%. If you meant "95% grid + 5% Oil & Dairy = 100% of 2,000 kcal",
  switch "Matrix percentages are a share of" to "The whole day" on the Plan page.

## Notes

- Editing an ingredient or recipe updates past log entries and every recipe that uses it.
- A food that's part of a recipe can't be deleted until it's removed from that recipe.
- Starter foods and two example recipes are added on first run. Set `DIET_SEED=0` before the first run to start empty.
- Set `SECRET_KEY` and `DIET_DB` environment variables to customize the session key and database path.
