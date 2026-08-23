

// Add Create sequenced assembly recipe for steel chain
// Texture created by Bigdog0408

ServerEvents.recipes(event => {
  console.info("=== 011_machines/sequenced_assembly Recipe script loaded ===");

  // Remove crafting table chain
  event.remove({ output: 'minecraft:chain' })

  // Create sequenced assembly using steel rods
  event.recipes.create.sequenced_assembly(
    [
      Item.of('minecraft:chain')
    ],
    '#forge:rods/steel', // ← changed here
    [
      event.recipes.create.pressing(
        'kubejs:incomplete_steel_chain',
        'kubejs:incomplete_steel_chain'
      ),
      
      event.recipes.create.deploying(
        'kubejs:incomplete_steel_chain',
        ['kubejs:incomplete_steel_chain', '#forge:nuggets/steel']
      ),
      
      event.recipes.create.pressing(
        'kubejs:incomplete_steel_chain',
        'kubejs:incomplete_steel_chain'
      )
    ]
  )
  .transitionalItem('kubejs:incomplete_steel_chain')
  .loops(2)
  .id('kubejs:steel_chain_seq')

})