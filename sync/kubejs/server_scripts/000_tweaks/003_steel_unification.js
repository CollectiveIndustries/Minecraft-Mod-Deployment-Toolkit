// 003_steel_unification.js

// ============================================================
// UNIFIED STEEL NUGGET TAG
// ============================================================

ServerEvents.tags('item', event => {
    event.add('forge:nuggets/steel', [
        'create_ironworks:steel_nugget',
        'tfmg:steel_nugget',
        'immersiveengineering:nugget_steel'
    ])
})


// ============================================================
// STEEL NUGGET RECIPE UNIFICATION
// ============================================================
//
// Normal recipes:
//     replaceInput() handles them.
//
// Nested recipes:
//     We directly modify the Gson JsonObject using Java methods.
//
// This specifically catches Create sequenced assembly recipes:
//
//     ingredient
//     ingredients
//     sequence[]
//         ingredient
//         ingredients
//         key
//
// We ONLY modify ingredient definitions.
// Recipe outputs/results are never touched.
// ============================================================

ServerEvents.recipes(event => {

    // --------------------------------------------------------
    // Steel nugget IDs that are being unified.
    // --------------------------------------------------------

    var steelNuggets = [
        'create_ironworks:steel_nugget',
        'tfmg:steel_nugget',
        'immersiveengineering:nugget_steel'
    ]


    // --------------------------------------------------------
    // Check a Gson JsonElement for one of the steel nugget IDs.
    // --------------------------------------------------------

    function isSteelNugget(element) {

        if (element == null) {
            return false
        }

        if (!element.isJsonPrimitive()) {
            return false
        }

        var id = element.getAsString()

        return steelNuggets.indexOf(id) !== -1
    }


    // --------------------------------------------------------
    // Replace one ingredient represented by a Gson JsonObject.
    //
    // BEFORE:
    //
    // {
    //   "item": "tfmg:steel_nugget"
    // }
    //
    // AFTER:
    //
    // {
    //   "tag": "forge:nuggets/steel"
    // }
    // --------------------------------------------------------

    function replaceIngredientObject(object) {

        if (object == null || !object.isJsonObject()) {
            return false
        }

        if (!object.has('item')) {
            return false
        }

        var itemElement = object.get('item')

        if (!isSteelNugget(itemElement)) {
            return false
        }

        // Use Gson's Java methods.
        // Do NOT use object.item / object[key].

        object.remove('item')
        object.addProperty('tag', 'forge:nuggets/steel')

        return true
    }


    // --------------------------------------------------------
    // Process an individual ingredient value.
    //
    // Handles:
    //
    // { "item": "..." }
    //
    // or:
    //
    // [
    //   { "item": "..." },
    //   { "tag": "..." }
    // ]
    // --------------------------------------------------------

    function replaceIngredientValue(element) {

        if (element == null) {
            return false
        }


        // Single ingredient object.

        if (element.isJsonObject()) {
            return replaceIngredientObject(element)
        }


        // Ingredient list.

        if (element.isJsonArray()) {

            var changed = false
            var array = element.getAsJsonArray()
            var iterator = array.iterator()

            while (iterator.hasNext()) {

                var child = iterator.next()

                if (replaceIngredientValue(child)) {
                    changed = true
                }
            }

            return changed
        }


        return false
    }


    // --------------------------------------------------------
    // Process a recipe object.
    //
    // IMPORTANT:
    // We do NOT recursively search every "item" property.
    //
    // That prevents recipe RESULTS from being changed.
    // --------------------------------------------------------

    function processRecipeObject(object) {

        if (object == null || !object.isJsonObject()) {
            return false
        }

        var changed = false


        // ----------------------------------------------------
        // Standard recipe input:
        //
        // "ingredient": {...}
        // ----------------------------------------------------

        if (object.has('ingredient')) {

            if (replaceIngredientValue(object.get('ingredient'))) {
                changed = true
            }
        }


        // ----------------------------------------------------
        // Standard processing inputs:
        //
        // "ingredients": [...]
        // ----------------------------------------------------

        if (object.has('ingredients')) {

            if (replaceIngredientValue(object.get('ingredients'))) {
                changed = true
            }
        }


        // ----------------------------------------------------
        // Vanilla shaped crafting:
        //
        // "key": {
        //     "A": {...},
        //     "B": {...}
        // }
        //
        // Each key value is an ingredient.
        // ----------------------------------------------------

        if (object.has('key')) {

            var keyObject = object.get('key')

            if (keyObject != null && keyObject.isJsonObject()) {

                var keyEntries = keyObject.getAsJsonObject().entrySet()
                var keyIterator = keyEntries.iterator()

                while (keyIterator.hasNext()) {

                    var keyEntry = keyIterator.next()

                    if (replaceIngredientValue(keyEntry.getValue())) {
                        changed = true
                    }
                }
            }
        }


        // ----------------------------------------------------
        // Create sequenced assembly:
        //
        // "sequence": [
        //     {
        //         "type": "create:deploying",
        //         "ingredients": [...]
        //     },
        //     ...
        // ]
        //
        // Each sequence step is itself a recipe JSON object.
        // ----------------------------------------------------

        if (object.has('sequence')) {

            var sequenceElement = object.get('sequence')

            if (
                sequenceElement != null &&
                sequenceElement.isJsonArray()
            ) {

                var sequence = sequenceElement.getAsJsonArray()
                var sequenceIterator = sequence.iterator()

                while (sequenceIterator.hasNext()) {

                    var step = sequenceIterator.next()

                    if (
                        step != null &&
                        step.isJsonObject()
                    ) {

                        if (processRecipeObject(step)) {
                            changed = true
                        }
                    }
                }
            }
        }


        return changed
    }


    // ========================================================
    // PASS 1
    //
    // KubeJS-native replacement for all recipe types that
    // expose their inputs through RecipeSchema.
    // ========================================================

    event.replaceInput(
        {},
        /^(create_ironworks:steel_nugget|tfmg:steel_nugget|immersiveengineering:nugget_steel)$/,
        '#forge:nuggets/steel'
    )


    // ========================================================
    // PASS 2
    //
    // Raw Gson JSON pass for nested recipe structures.
    // ========================================================

    event.forEachRecipe({}, recipe => {

        try {

            var recipeJson = recipe.json

            if (
                recipeJson == null ||
                !recipeJson.isJsonObject()
            ) {
                return
            }


            if (!processRecipeObject(recipeJson)) {
                return
            }


            // The raw Gson JSON has now been changed.
            //
            // Synchronize KubeRecipe's component values with the
            // modified JSON, then mark it changed so KubeJS will
            // serialize it during applyChanges().

            recipe.deserialize(true)
            recipe.save()


            console.log(
                '[STEEL UNIFICATION] Modified: ' +
                recipe.getId()
            )

        } catch (error) {

            console.error(
                '[STEEL UNIFICATION] FAILED: ' +
                recipe.getId() +
                ' -> ' +
                error
            )
        }
    })
})
