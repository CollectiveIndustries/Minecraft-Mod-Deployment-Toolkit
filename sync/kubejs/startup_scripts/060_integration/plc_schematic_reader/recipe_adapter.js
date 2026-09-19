/*
 * ============================================================
 * PLC RECIPE ADAPTER
 * ============================================================
 *
 * Minecraft:
 *     1.20.1 Forge
 *
 * KubeJS:
 *     2001.6.5-build.26
 *
 * Create:
 *     6.0.8
 *
 * PURPOSE
 * ============================================================
 *
 * DATA ADAPTER ONLY.
 *
 * Lua PLC owns:
 *
 *     - planning
 *     - recipe selection
 *     - inventory decisions
 *     - industrial fallback
 *     - execution
 *     - Create package deployment
 *
 * This adapter owns:
 *
 *     - Minecraft RecipeManager access
 *     - minecraft:crafting recipes
 *     - recipe outputs
 *     - recipe ingredient positions
 *     - ingredient alternatives
 *     - tag membership queries
 *     - item existence queries
 *
 * ============================================================
 * PERIPHERAL METHODS
 * ============================================================
 *
 *   canCraft(itemId)
 *       Returns { success, craftable, recipeCount, recipes }
 *
 *   getRecipe(itemId)
 *       Returns the same shape as canCraft, with the full
 *       recipe set for the given item.
 *
 *   resolveTag(tagName)
 *       Returns an array of { item = "<registry:name>", count = 1 }
 *       for every member of the given item tag.
 *
 *       Accepts tag names with or without the "#" prefix:
 *
 *           resolveTag("minecraft:logs_that_burn")
 *           resolveTag("#forge:ingots/steel")
 *
 *       Returns an empty array if the tag is unknown, empty,
 *       malformed, or if the registry rejects the query.
 *       Never raises.
 *
 *   itemExists(itemId)
 *       Returns true if the given registry ID is a real item
 *       in the current item registry. False otherwise, never
 *       raises.
 *
 * ============================================================
 * CRITICAL GRID MODEL
 * ============================================================
 *
 * Minecraft shaped recipes contain an ordered pattern.
 *
 * The pattern is NOT automatically equivalent to Create's
 * physical 3x3 slot numbering.
 *
 * A 1x2 recipe:
 *
 *     P
 *     P
 *
 * is centered in the 3x3 grid as:
 *
 *     . P .
 *     . P .
 *     . . .
 *
 * therefore:
 *
 *     pattern 1 -> slot 2
 *     pattern 2 -> slot 5
 *
 * A 3x3 recipe keeps its original positions.
 *
 * The adapter exports TWO compatible representations:
 *
 *     recipe.ingredients[]
 *
 * Each occupied ingredient entry contains:
 *
 *     slot
 *     row
 *     column
 *     patternIndex
 *     ingredient data
 *
 *
 *     recipe.slots[1..9]
 *
 * This is the authoritative physical Create grid.
 *
 * Lua must use the explicit slot value.
 *
 * ============================================================
 */


var ResourceLocationClass =
    Java.loadClass(
        'net.minecraft.resources.ResourceLocation'
    )


var RecipeTypeClass =
    Java.loadClass(
        'net.minecraft.world.item.crafting.RecipeType'
    )


var BuiltInRegistriesClass =
    Java.loadClass(
        'net.minecraft.core.registries.BuiltInRegistries'
    )


var TagKeyClass =
    Java.loadClass(
        'net.minecraft.tags.TagKey'
    )


var RegistriesClass =
    Java.loadClass(
        'net.minecraft.core.registries.Registries'
    )


var PLC_ID =
    'kubejs:plc_schematic_reader'


var PERIPHERAL_TYPE =
    'plc_recipe_adapter'


var MAX_RECIPE_RESULTS =
    64


var MAX_TAG_ALTERNATIVES =
    256


var CRAFTING_GRID_WIDTH =
    3


var CRAFTING_GRID_HEIGHT =
    3


var CRAFTING_GRID_SLOTS =
    9


function plcSafeString(value) {

    if (
        value === null ||
        value === undefined
    ) {

        return null
    }

    return String(value)
}


function plcResourceLocation(id) {

    var text =
        plcSafeString(
            id
        )


    if (text === null) {

        return null
    }


    /*
     * Strip an optional leading "#" so that tag names
     * passed through plcResourceLocation resolve cleanly.
     * (Item registry lookups never see the "#" character
     * in a valid item id, but tag queries commonly do.)
     */

    if (
        text.length > 0 &&
        text.charAt(0) === '#'
    ) {

        text =
            text.substring(
                1
            )

    }


    try {

        return ResourceLocationClass
            .tryParse(
                text
            )

    } catch (error) {

        return null

    }

}


function plcItemId(stack) {

    if (
        stack === null ||
        stack === undefined
    ) {

        return null
    }


    try {

        if (
            stack.isEmpty()
        ) {

            return null

        }

    } catch (error) {

        return null

    }


    try {

        var item =
            stack.getItem()


        var id =
            BuiltInRegistriesClass.ITEM
                .getKey(
                    item
                )


        if (
            id === null ||
            id === undefined
        ) {

            return null
        }


        return String(
            id
        )

    } catch (error) {

        console.error(
            '[PLC RECIPE] Failed to resolve item id: ' +
            error
        )

        return null

    }

}


function plcSerializeStack(stack) {

    if (
        stack === null ||
        stack === undefined
    ) {

        return null
    }


    try {

        if (
            stack.isEmpty()
        ) {

            return null

        }

    } catch (error) {

        return null

    }


    var itemId =
        plcItemId(
            stack
        )


    if (
        itemId === null
    ) {

        return null
    }


    var result = {

        item:
            itemId,

        count:
            stack.getCount()

    }


    try {

        var tag =
            stack.getTag()


        if (
            tag !== null &&
            tag !== undefined
        ) {

            result.nbt =
                String(
                    tag
                )

        }

    } catch (error) {

    }


    return result

}


function plcJsonString(json) {

    if (
        json === null ||
        json === undefined
    ) {

        return null
    }


    try {

        return String(
            json.toString()
        )

    } catch (error) {

        return null

    }

}


function plcResolveItemTag(tagId) {

    var result = []


    if (
        tagId === null ||
        tagId === undefined
    ) {

        return result

    }


    try {

        var location =
            plcResourceLocation(
                String(tagId)
            )


        if (
            location === null ||
            location === undefined
        ) {

            return result

        }


        var tagKey =
            TagKeyClass
                .create(
                    RegistriesClass.ITEM,
                    location
                )


        var holderSetOptional =
            BuiltInRegistriesClass.ITEM
                .getTag(
                    tagKey
                )


        if (
            holderSetOptional === null ||
            holderSetOptional === undefined
        ) {

            return result

        }


        if (
            !holderSetOptional.isPresent()
        ) {

            return result

        }


        var holderSet =
            holderSetOptional.get()


        var iterator =
            holderSet.iterator()


        var count =
            0


        while (
            iterator.hasNext() &&
            count < MAX_TAG_ALTERNATIVES
        ) {

            var holder =
                iterator.next()


            if (
                holder === null ||
                holder === undefined
            ) {

                continue

            }


            var item =
                holder.value()


            if (
                item === null ||
                item === undefined
            ) {

                continue

            }


            var itemId =
                BuiltInRegistriesClass.ITEM
                    .getKey(
                        item
                    )


            if (
                itemId === null ||
                itemId === undefined
            ) {

                continue

            }


            result.push({

                item:
                    String(
                        itemId
                    ),

                count:
                    1

            })


            count++

        }

    } catch (error) {

        console.error(
            '[PLC RECIPE] Failed to resolve item tag ' +
            String(tagId) +
            ': ' +
            error
        )

    }


    return result

}


function plcIngredientJsonToData(
    jsonElement
) {

    var result = {

        alternatives: []

    }


    if (
        jsonElement === null ||
        jsonElement === undefined
    ) {

        return result

    }


    try {

        if (
            jsonElement.isJsonArray()
        ) {

            var array =
                jsonElement.getAsJsonArray()


            for (
                var i = 0;
                i < array.size();
                i++
            ) {

                if (
                    result.alternatives.length >=
                    MAX_TAG_ALTERNATIVES
                ) {

                    break

                }


                var child =
                    array.get(
                        i
                    )


                var childData =
                    plcIngredientJsonToData(
                        child
                    )


                for (
                    var j = 0;
                    j < childData.alternatives.length;
                    j++
                ) {

                    result.alternatives.push(
                        childData.alternatives[j]
                    )


                    if (
                        result.alternatives.length >=
                        MAX_TAG_ALTERNATIVES
                    ) {

                        break

                    }

                }

            }


            return result

        }


        if (
            jsonElement.isJsonPrimitive()
        ) {

            var primitive =
                String(
                    jsonElement.getAsString()
                )


            var itemLocation =
                plcResourceLocation(
                    primitive
                )


            if (
                itemLocation !== null
            ) {

                var registryItem =
                    BuiltInRegistriesClass.ITEM
                        .get(
                            itemLocation
                        )


                if (
                    registryItem !== null &&
                    registryItem !== undefined
                ) {

                    result.alternatives.push({

                        item:
                            String(
                                itemLocation
                            ),

                        count:
                            1

                    })

                }

            }


            return result

        }


        if (
            jsonElement.isJsonObject()
        ) {

            var object =
                jsonElement.getAsJsonObject()


            if (
                object.has(
                    'item'
                )
            ) {

                var itemId =
                    String(
                        object
                            .get(
                                'item'
                            )
                            .getAsString()
                    )


                var count =
                    object.has(
                        'count'
                    )
                        ? object
                            .get(
                                'count'
                            )
                            .getAsInt()
                        : 1


                result.alternatives.push({

                    item:
                        itemId,

                    count:
                        count

                })


                return result

            }


            if (
                object.has(
                    'tag'
                )
            ) {

                var tagId =
                    String(
                        object
                            .get(
                                'tag'
                            )
                            .getAsString()
                    )


                var tagAlternatives =
                    plcResolveItemTag(
                        tagId
                    )


                for (
                    var k = 0;
                    k < tagAlternatives.length;
                    k++
                ) {

                    result.alternatives.push(
                        tagAlternatives[k]
                    )

                }


                if (
                    result.alternatives.length === 0
                ) {

                    result.tag =
                        tagId

                }


                return result

            }


            if (
                object.has(
                    'type'
                )
            ) {

                result.type =
                    String(
                        object
                            .get(
                                'type'
                            )
                            .getAsString()
                    )


                result.json =
                    plcJsonString(
                        object
                    )

            }


            return result

        }

    } catch (error) {

        console.error(
            '[PLC RECIPE] Ingredient JSON conversion failed: ' +
            error
        )

    }


    return result

}


function plcSerializeIngredient(
    ingredient
) {

    var result = {

        alternatives: []

    }


    if (
        ingredient === null ||
        ingredient === undefined
    ) {

        return result

    }


    try {

        var json =
            ingredient.toJson()


        return plcIngredientJsonToData(
            json
        )

    } catch (error) {

        console.error(
            '[PLC RECIPE] Failed to serialize ingredient: ' +
            error
        )


        result.error =
            String(
                error
            )


        return result

    }

}


function plcRecipeId(recipe) {

    if (
        recipe === null ||
        recipe === undefined
    ) {

        return null

    }


    try {

        var id =
            recipe.getId()


        if (
            id !== null &&
            id !== undefined
        ) {

            return String(
                id
            )

        }

    } catch (error) {

    }


    return null

}


function plcRecipeDimensions(
    recipe
) {

    var width =
        null


    var height =
        null


    try {

        if (
            typeof recipe.getWidth ===
            'function'
        ) {

            width =
                Number(
                    recipe.getWidth()
                )

        }

    } catch (error) {

    }


    try {

        if (
            typeof recipe.getHeight ===
            'function'
        ) {

            height =
                Number(
                    recipe.getHeight()
                )

        }

    } catch (error) {

    }


    return {

        width:
            width,

        height:
            height

    }

}


function plcNormalizeDimension(
    value,
    fallback
) {

    var result =
        Number(
            value
        )


    if (
        !isFinite(result) ||
        result <= 0
    ) {

        return fallback

    }


    result =
        Math.floor(
            result
        )


    return result

}


function plcCenteredOffset(
    width,
    height
) {

    var offsetX =
        Math.floor(
            (
                CRAFTING_GRID_WIDTH
                -
                width
            )
            /
            2
        )


    var offsetY =
        Math.floor(
            (
                CRAFTING_GRID_HEIGHT
                -
                height
            )
            /
            2
        )


    return {

        x:
            Math.max(
                0,
                offsetX
            ),

        y:
            Math.max(
                0,
                offsetY
            )

    }

}


/* ============================================================
 * BUILD SHAPED GRID
 * ============================================================
 */

function plcBuildShapedGrid(
    recipe,
    ingredients,
    width,
    height
) {

    var slots =
        []


    for (
        var i = 1;
        i <= CRAFTING_GRID_SLOTS;
        i++
    ) {

        slots[i] =
            null

    }


    width =
        plcNormalizeDimension(
            width,
            3
        )


    height =
        plcNormalizeDimension(
            height,
            3
        )


    if (
        width > CRAFTING_GRID_WIDTH ||
        height > CRAFTING_GRID_HEIGHT
    ) {

        throw new Error(
            'Crafting recipe exceeds 3x3 grid: ' +
            width +
            'x' +
            height
        )

    }


    var offset =
        plcCenteredOffset(
            width,
            height
        )


    for (
        var row = 0;
        row < height;
        row++
    ) {

        for (
            var column = 0;
            column < width;
            column++
        ) {

            var patternIndex =
                (
                    row * width
                )
                +
                column


            var ingredient =
                ingredients[
                    patternIndex
                ]


            if (
                ingredient === null ||
                ingredient === undefined
            ) {

                continue

            }


            /*
             * Empty Ingredient objects are not physical grid
             * entries.
             */

            if (
                !ingredient.alternatives ||
                ingredient.alternatives.length === 0
            ) {

                continue

            }


            var destinationRow =
                offset.y
                +
                row


            var destinationColumn =
                offset.x
                +
                column


            var slotIndex =
                (
                    destinationRow
                    *
                    CRAFTING_GRID_WIDTH
                )
                +
                destinationColumn
                +
                1


            if (
                slotIndex < 1 ||
                slotIndex > CRAFTING_GRID_SLOTS
            ) {

                continue

            }


            slots[
                slotIndex
            ] = {

                slot:
                    slotIndex,

                row:
                    destinationRow + 1,

                column:
                    destinationColumn + 1,

                patternRow:
                    row + 1,

                patternColumn:
                    column + 1,

                patternIndex:
                    patternIndex + 1,

                ingredient:
                    ingredient

            }

        }

    }


    return slots

}


/* ============================================================
 * BUILD SHAPELESS GRID
 * ============================================================
 */

function plcBuildShapelessGrid(
    ingredients
) {

    var slots =
        []


    for (
        var i = 1;
        i <= CRAFTING_GRID_SLOTS;
        i++
    ) {

        slots[i] =
            null

    }


    var usableIngredients =
        []


    for (
        var index = 0;
        index < ingredients.length;
        index++
    ) {

        var ingredient =
            ingredients[
                index
            ]


        if (
            ingredient === null ||
            ingredient === undefined
        ) {

            continue

        }


        if (
            !ingredient.alternatives ||
            ingredient.alternatives.length === 0
        ) {

            continue

        }


        usableIngredients.push(
            ingredient
        )

    }


    var ingredientCount =
        usableIngredients.length


    if (
        ingredientCount <= 0
    ) {

        return slots

    }


    if (
        ingredientCount > CRAFTING_GRID_SLOTS
    ) {

        throw new Error(
            'Shapeless recipe contains more than 9 ingredient entries.'
        )

    }


    var width =
        1


    var height =
        ingredientCount


    if (
        ingredientCount >= 3
    ) {

        width =
            3

        height =
            Math.ceil(
                ingredientCount / 3
            )

    } else if (
        ingredientCount === 2
    ) {

        width =
            2

        height =
            1

    }


    var offset =
        plcCenteredOffset(
            width,
            height
        )


    for (
        var index = 0;
        index < ingredientCount;
        index++
    ) {

        var row =
            Math.floor(
                index / width
            )


        var column =
            index % width


        var destinationRow =
            offset.y
            +
            row


        var destinationColumn =
            offset.x
            +
            column


        var slotIndex =
            (
                destinationRow
                *
                CRAFTING_GRID_WIDTH
            )
            +
            destinationColumn
            +
            1


        var ingredient =
            usableIngredients[
                index
            ]


        slots[
            slotIndex
        ] = {

            slot:
                slotIndex,

            row:
                destinationRow + 1,

            column:
                destinationColumn + 1,

            patternRow:
                row + 1,

            patternColumn:
                column + 1,

            patternIndex:
                index + 1,

            ingredient:
                ingredient

        }

    }


    return slots

}


/* ============================================================
 * SERIALIZE INGREDIENT LIST
 * ============================================================
 */

function plcSerializeIngredients(
    recipe
) {

    var result =
        []


    var rawIngredients =
        recipe.getIngredients()


    var iterator =
        rawIngredients.iterator()


    while (
        iterator.hasNext()
    ) {

        var ingredient =
            iterator.next()


        result.push(
            plcSerializeIngredient(
                ingredient
            )
        )

    }


    return result

}


/* ============================================================
 * RECIPE SHAPE
 * ============================================================
 */

function plcRecipeShapeType(
    recipe,
    dimensions
) {

    if (
        dimensions.width !== null
        &&
        dimensions.height !== null
        &&
        dimensions.width > 0
        &&
        dimensions.height > 0
    ) {

        return 'shaped'

    }


    return 'shapeless'

}


/* ============================================================
 * APPLY PHYSICAL POSITIONS TO INGREDIENTS
 * ============================================================
 *
 * THIS IS THE IMPORTANT COMPATIBILITY FIX.
 *
 * The old adapter exported:
 *
 *     ingredients[1]
 *     ingredients[2]
 *
 * and separately:
 *
 *     slots[2]
 *     slots[5]
 *
 * Some Lua code naturally consumed the first representation
 * and incorrectly assumed:
 *
 *     ingredient 1 -> slot 1
 *     ingredient 2 -> slot 2
 *
 * That caused the exact failure observed in the factory.
 *
 * We now copy the authoritative physical position directly
 * onto every ingredient entry.
 *
 * Therefore:
 *
 *     ingredient.slot
 *
 * is always the actual Create 3x3 slot.
 *
 * ============================================================
 */

function plcAttachIngredientPositions(
    ingredients,
    slots
) {

    var positioned =
        []


    for (
        var i = 0;
        i < ingredients.length;
        i++
    ) {

        var source =
            ingredients[
                i
            ]


        var copy = {}


        for (
            var key in source
        ) {

            copy[
                key
            ] =
                source[
                    key
                ]

        }


        /*
         * Find the physical slot associated with this
         * pattern index.
         */

        var physical =
            null


        for (
            var slotIndex = 1;
            slotIndex <= CRAFTING_GRID_SLOTS;
            slotIndex++
        ) {

            var slot =
                slots[
                    slotIndex
                ]


            if (
                slot === null ||
                slot === undefined
            ) {

                continue

            }


            if (
                slot.patternIndex ===
                i + 1
            ) {

                physical =
                    slot

                break

            }

        }


        if (
            physical !== null
        ) {

            copy.slot =
                physical.slot

            copy.gridSlot =
                physical.slot

            copy.row =
                physical.row

            copy.column =
                physical.column

            copy.patternRow =
                physical.patternRow

            copy.patternColumn =
                physical.patternColumn

            copy.patternIndex =
                physical.patternIndex

        } else {

            /*
             * An empty shaped pattern cell remains an ingredient
             * entry but is explicitly marked as unplaced.
             */

            copy.slot =
                null

            copy.gridSlot =
                null

            copy.row =
                null

            copy.column =
                null

            copy.patternRow =
                Math.floor(
                    i /
                    (
                        ingredients.length > 0
                            ? 1
                            : 1
                    )
                )
                + 1

            copy.patternColumn =
                null

            copy.patternIndex =
                i + 1

        }


        positioned.push(
            copy
        )

    }


    return positioned

}


/* ============================================================
 * RECIPE SERIALIZATION
 * ============================================================
 */

function plcSerializeRecipe(
    recipe,
    level
) {

    if (
        recipe === null ||
        recipe === undefined
    ) {

        return null

    }


    var outputStack


    try {

        outputStack =
            recipe.getResultItem(
                level.registryAccess()
            )

    } catch (error) {

        console.error(
            '[PLC RECIPE DEBUG] getResultItem failed: ' +
            error
        )

        return null

    }


    if (
        outputStack === null ||
        outputStack === undefined
    ) {

        return null

    }


    var outputId =
        plcItemId(
            outputStack
        )


    if (
        outputId === null
    ) {

        return null

    }


    var ingredients


    try {

        ingredients =
            plcSerializeIngredients(
                recipe
            )

    } catch (error) {

        console.error(
            '[PLC RECIPE] Failed to read recipe ingredient list: ' +
            error
        )

        return null

    }


    var dimensions =
        plcRecipeDimensions(
            recipe
        )


    var shapeType =
        plcRecipeShapeType(
            recipe,
            dimensions
        )


    var width =
        dimensions.width


    var height =
        dimensions.height


    var slots


    if (
        shapeType === 'shaped'
    ) {

        slots =
            plcBuildShapedGrid(
                recipe,
                ingredients,
                width,
                height
            )

    } else {

        slots =
            plcBuildShapelessGrid(
                ingredients
            )

    }


    /*
     * Attach authoritative physical positions to the
     * ingredient entries themselves.
     */

    var positionedIngredients =
        plcAttachIngredientPositions(
            ingredients,
            slots
        )


    /*
     * Serialize physical 3x3 grid.
     */

    var serializedSlots =
        []


    for (
        var slotIndex = 1;
        slotIndex <= CRAFTING_GRID_SLOTS;
        slotIndex++
    ) {

        var slot =
            slots[
                slotIndex
            ]


        if (
            slot === null ||
            slot === undefined
        ) {

            serializedSlots[
                slotIndex
            ] =
                null

            continue

        }


        serializedSlots[
            slotIndex
        ] = {

            slot:
                slot.slot,

            row:
                slot.row,

            column:
                slot.column,

            patternRow:
                slot.patternRow,

            patternColumn:
                slot.patternColumn,

            patternIndex:
                slot.patternIndex,

            ingredient:
                slot.ingredient

        }

    }


    /*
     * ========================================================
     * DIAGNOSTIC
     * ========================================================
     */

    console.info(
        '[PLC RECIPE] ' +
        outputId +
        ' recipe=' +
        String(
            plcRecipeId(
                recipe
            )
        ) +
        ' shape=' +
        String(
            shapeType
        ) +
        ' size=' +
        String(
            width
        ) +
        'x' +
        String(
            height
        )
    )


    for (
        var diagnosticIndex = 0;
        diagnosticIndex < positionedIngredients.length;
        diagnosticIndex++
    ) {

        var diagnosticIngredient =
            positionedIngredients[
                diagnosticIndex
            ]


        if (
            diagnosticIngredient.slot !== null &&
            diagnosticIngredient.slot !== undefined
        ) {

            console.info(
                '[PLC RECIPE] ingredient ' +
                String(
                    diagnosticIndex + 1
                ) +
                ' -> slot ' +
                String(
                    diagnosticIngredient.slot
                ) +
                ' pattern=' +
                String(
                    diagnosticIngredient.patternIndex
                )
            )

        } else {

            console.info(
                '[PLC RECIPE] ingredient ' +
                String(
                    diagnosticIndex + 1
                ) +
                ' -> NO PHYSICAL SLOT'
            )

        }

    }


    for (
        var physicalDiagnosticSlot = 1;
        physicalDiagnosticSlot <= CRAFTING_GRID_SLOTS;
        physicalDiagnosticSlot++
    ) {

        var physicalEntry =
            serializedSlots[
                physicalDiagnosticSlot
            ]


        if (
            physicalEntry === null ||
            physicalEntry === undefined
        ) {

            continue

        }


        console.info(
            '[PLC RECIPE] slot ' +
            String(
                physicalDiagnosticSlot
            ) +
            ' <- pattern ' +
            String(
                physicalEntry.patternIndex
            )
        )

    }


    return {

        id:
            plcRecipeId(
                recipe
            ),

        type:
            'minecraft:crafting',

        shapeType:
            shapeType,

        output:
            plcSerializeStack(
                outputStack
            ),

        /*
         * IMPORTANT:
         *
         * These entries now contain explicit physical slot
         * information.
         */

        ingredients:
            positionedIngredients,

        /*
         * Authoritative full 3x3 physical grid.
         */

        slots:
            serializedSlots,

        width:
            width,

        height:
            height

    }

}


/* ============================================================
 * RECIPE ENUMERATION
 * ============================================================
 */

function plcGetCraftingRecipes(
    recipeManager
) {

    try {

        return recipeManager
            .getAllRecipesFor(
                RecipeTypeClass.CRAFTING
            )

    } catch (error) {

        throw new Error(
            'Unable to enumerate crafting recipes: ' +
            error
        )

    }

}


/* ============================================================
 * FIND RECIPES
 * ============================================================
 */

function plcFindRecipes(
    block,
    args
) {

    if (
        args === null ||
        args === undefined ||
        args.length < 1
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            error:
                'getRecipe requires an item id'

        }

    }


    var requestedId =
        plcSafeString(
            args[0]
        )


    if (
        requestedId === null ||
        requestedId.length === 0
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            error:
                'item id cannot be empty'

        }

    }


    var requestedLocation =
        plcResourceLocation(
            requestedId
        )


    if (
        requestedLocation === null
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'invalid item id: ' +
                requestedId

        }

    }


    var level


    try {

        level =
            block.getLevel()

    } catch (error) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'Minecraft level unavailable: ' +
                error

        }

    }


    if (
        level === null ||
        level === undefined
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'Minecraft level unavailable'

        }

    }


    var recipeManager


    try {

        recipeManager =
            level.getRecipeManager()

    } catch (error) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'RecipeManager unavailable: ' +
                error

        }

    }


    if (
        recipeManager === null ||
        recipeManager === undefined
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'RecipeManager unavailable'

        }

    }


    var craftingRecipes


    try {

        craftingRecipes =
            plcGetCraftingRecipes(
                recipeManager
            )

    } catch (error) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                String(
                    error
                )

        }

    }


    if (
        craftingRecipes === null ||
        craftingRecipes === undefined
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'Crafting recipe collection unavailable'

        }

    }


    var recipes =
        []


    var iterator


    try {

        iterator =
            craftingRecipes.iterator()

    } catch (error) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId,

            error:
                'Unable to iterate crafting recipes: ' +
                error

        }

    }


    var inspected =
        0


    while (
        iterator.hasNext()
    ) {

        if (
            recipes.length >=
            MAX_RECIPE_RESULTS
        ) {

            break

        }


        inspected++


        var recipe


        try {

            recipe =
                iterator.next()

        } catch (error) {

            continue

        }


        if (
            recipe === null ||
            recipe === undefined
        ) {

            continue

        }


        var output


        try {

            output =
                recipe.getResultItem(
                    level.registryAccess()
                )

        } catch (error) {

            continue

        }


        if (
            output === null ||
            output === undefined
        ) {

            continue

        }


        try {

            if (
                output.isEmpty()
            ) {

                continue

            }

        } catch (error) {

            continue

        }


        var outputLocation


        try {

            outputLocation =
                BuiltInRegistriesClass.ITEM
                    .getKey(
                        output.getItem()
                    )

        } catch (error) {

            continue

        }


        if (
            outputLocation === null ||
            outputLocation === undefined
        ) {

            continue

        }


        if (
            String(
                outputLocation
            )
            !==
            String(
                requestedLocation
            )
        ) {

            continue

        }


        var serialized


        try {

            serialized =
                plcSerializeRecipe(
                    recipe,
                    level
                )

        } catch (error) {

            console.error(
                '[PLC RECIPE DEBUG] Recipe serialization failed: ' +
                error
            )

            serialized =
                null

        }


        if (
            serialized !== null
        ) {

            recipes.push(
                serialized
            )

        }

    }


    var found =
        recipes.length > 0


    if (!found) {

        console.info(
            '[PLC RECIPE DEBUG] No matching crafting recipes for ' +
            requestedId +
            ' after inspecting ' +
            inspected +
            ' recipes'
        )


        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0,

            recipes:
                [],

            item:
                requestedId

        }

    }


    return {

        success:
            true,

        craftable:
            true,

        recipeCount:
            recipes.length,

        recipes:
            recipes,

        item:
            requestedId

    }

}


/* ============================================================
 * CAN CRAFT
 * ============================================================
 */

function plcCanCraft(
    block,
    args
) {

    var result =
        plcFindRecipes(
            block,
            args
        )


    if (
        result === null ||
        result === undefined
    ) {

        return {

            success:
                false,

            craftable:
                false,

            recipeCount:
                0

        }

    }


    return {

        success:
            result.success,

        craftable:
            result.craftable,

        recipeCount:
            result.recipeCount,

        item:
            result.item,

        recipes:
            result.recipes

    }

}


/* ============================================================
 * GET RECIPE
 * ============================================================
 */

function plcGetRecipe(
    block,
    args
) {

    return plcFindRecipes(
        block,
        args
    )

}


/* ============================================================
 * RESOLVE TAG
 * ============================================================
 *
 * Returns the concrete item members of an item tag.
 *
 * The result is a plain array of { item, count } entries.
 * Every count is 1; a tag represents "any one of these
 * items", and downstream recipe sizing determines quantity.
 *
 * Accepts tag names with or without the leading "#".
 *
 * Never raises. Returns an empty array on any failure:
 *
 *     - missing or malformed tag name
 *     - tag not present in the item registry
 *     - tag present but empty
 *     - any internal registry exception
 *
 * The adapter has no way to distinguish an empty tag from
 * an unknown one from the caller's perspective. If the Lua
 * side needs that distinction, add a corresponding
 * "tagExists" method using the same registry lookup
 * pattern as plcItemExistsRequest below.
 *
 * ============================================================
 */

function plcResolveTagRequest(tagId) {

    if (
        tagId === null ||
        tagId === undefined
    ) {

        return []

    }


    return plcResolveItemTag(
        String(
            tagId
        )
    )

}


/* ============================================================
 * ITEM EXISTS
 * ============================================================
 *
 * Returns true if the given registry ID resolves to a real
 * item in the current item registry.
 *
 * Accepts registry IDs with the standard "namespace:name"
 * format. Bare names without a namespace are treated as
 * "minecraft:<name>" by plcResourceLocation, which matches
 * Minecraft's own defaulting behaviour.
 *
 * Never raises. Returns false on any failure.
 *
 * ============================================================
 */

function plcItemExistsRequest(itemId) {

    if (
        itemId === null ||
        itemId === undefined
    ) {

        return false

    }


    var location =
        plcResourceLocation(
            String(
                itemId
            )
        )


    if (
        location === null ||
        location === undefined
    ) {

        return false

    }


    try {

        return BuiltInRegistriesClass.ITEM
            .containsKey(
                location
            )

    } catch (error) {

        console.error(
            '[PLC RECIPE] itemExists lookup failed for ' +
            String(itemId) +
            ': ' +
            error
        )

        return false

    }

}


/* ============================================================
 * PERIPHERAL REGISTRATION
 * ============================================================
 */

ComputerCraftEvents.peripheral(
    function(event) {

        var plc =
            event.registerPeripheral(
                PERIPHERAL_TYPE,
                PLC_ID
            )


        plc.mainThreadMethod(
            'canCraft',

            function(
                block,
                side,
                args,
                computer,
                context
            ) {

                return plcCanCraft(
                    block,
                    args
                )

            }
        )


        plc.mainThreadMethod(
            'getRecipe',

            function(
                block,
                side,
                args,
                computer,
                context
            ) {

                return plcGetRecipe(
                    block,
                    args
                )

            }
        )


        plc.mainThreadMethod(
            'resolveTag',

            function(
                block,
                side,
                args,
                computer,
                context
            ) {

                if (
                    args === null ||
                    args === undefined ||
                    args.length < 1
                ) {

                    return []

                }


                return plcResolveTagRequest(
                    args[0]
                )

            }
        )


        plc.mainThreadMethod(
            'itemExists',

            function(
                block,
                side,
                args,
                computer,
                context
            ) {

                if (
                    args === null ||
                    args === undefined ||
                    args.length < 1
                ) {

                    return false

                }


                return plcItemExistsRequest(
                    args[0]
                )

            }
        )

    }
)


/* ============================================================
 * STARTUP REPORT
 * ============================================================
 */

console.info(
    '[PLC RECIPE] ========================================='
)

console.info(
    '[PLC RECIPE] RECIPE ADAPTER'
)

console.info(
    '[PLC RECIPE] Registered peripheral: ' +
    PERIPHERAL_TYPE
)

console.info(
    '[PLC RECIPE] Target block: ' +
    PLC_ID
)

console.info(
    '[PLC RECIPE] Method: canCraft(item)'
)

console.info(
    '[PLC RECIPE] Method: getRecipe(item)'
)

console.info(
    '[PLC RECIPE] Method: resolveTag(tagName)'
)

console.info(
    '[PLC RECIPE] Method: itemExists(itemId)'
)

console.info(
    '[PLC RECIPE] Source: Minecraft RecipeManager'
)

console.info(
    '[PLC RECIPE] Recipe type: minecraft:crafting'
)

console.info(
    '[PLC RECIPE] Grid: 3x3 crafting'
)

console.info(
    '[PLC RECIPE] Shaped recipes: centered physical slots'
)

console.info(
    '[PLC RECIPE] Ingredient entries: explicit physical slot'
)

console.info(
    '[PLC RECIPE] Shapeless recipes: deterministic centered layout'
)

console.info(
    '[PLC RECIPE] Recipe objects: direct'
)

console.info(
    '[PLC RECIPE] Recipe collection access: iterator'
)

console.info(
    '[PLC RECIPE] Output IDs: BuiltInRegistries.ITEM'
)

console.info(
    '[PLC RECIPE] Ingredient source: Ingredient.toJson()'
)

console.info(
    '[PLC RECIPE] Tag resolution: BuiltInRegistries.ITEM'
)

console.info(
    '[PLC RECIPE] Physical recipe placement: adapter'
)

console.info(
    '[PLC RECIPE] Planner fallback position: NOT authoritative'
)

console.info(
    '[PLC RECIPE] Planning: LUA'
)

console.info(
    '[PLC RECIPE] Industrial fallback: LUA database'
)

console.info(
    '[PLC RECIPE] Create package deployment: Stock Ticker API'
)

console.info(
    '[PLC RECIPE] ========================================='
)