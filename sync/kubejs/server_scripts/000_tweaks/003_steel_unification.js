// steel_nuggets.js

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
// UNIFY ALL RECIPE INPUTS
// ============================================================

ServerEvents.recipes(event => {

    event.replaceInput(
        {},
        'create_ironworks:steel_nugget',
        '#forge:nuggets/steel'
    )

    event.replaceInput(
        {},
        'tfmg:steel_nugget',
        '#forge:nuggets/steel'
    )

    event.replaceInput(
        {},
        'immersiveengineering:nugget_steel',
        '#forge:nuggets/steel'
    )

})
