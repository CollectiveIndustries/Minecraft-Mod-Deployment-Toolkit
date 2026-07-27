ServerEvents.recipes(event => {

  // --------------------------------------------------------
  // Filling: Treated Wood + Creosote → Hardened Planks
  // --------------------------------------------------------
  event.custom({
    type: 'create:filling',
    ingredients: [
      { tag: 'forge:treated_wood' },
      { fluid: 'immersiveengineering:creosote', amount: 200 }
    ],
    results: [
      { item: 'tfmg:hardened_planks' }
    ]
  }).id('kubejs:create_hardend_planks');

  // --------------------------------------------------------
  // Mechanical Crafting: Tracks (replacing default recipe)
  // --------------------------------------------------------
  event.recipes.create.mechanical_crafting(
    'create:track',
    [
      'FSTSFFSTSF',
      'FSTSFFSTSF'
    ],
    {
      F: 'immersiveengineering:treated_fence',
      S: 'tfmg:screw',
      T: 'immersiveengineering:stick_steel'
    }
  ).id('kubejs:tracks');

  // --------------------------------------------------------
  // IE Blast Furnace Fuels
  // --------------------------------------------------------
  event.custom({
    type: 'immersiveengineering:blast_furnace_fuel',
    input: { item: 'tfmg:coal_coke' },
    burnTime: 1200
  }).id('kubejs:create_coalcoke');

  event.custom({
    type: 'immersiveengineering:blast_furnace_fuel',
    input: { item: 'tfmg:coal_coke_block' },
    burnTime: 12000
  }).id('kubejs:create_coalcoke_block');

});