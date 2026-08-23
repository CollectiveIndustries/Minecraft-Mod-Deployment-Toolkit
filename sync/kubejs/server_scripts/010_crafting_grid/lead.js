ServerEvents.recipes(event => {
  console.info("=== 010_crafting_grid/lead Recipe script loaded ===");  

  // Add new rod-based recipe
  event.shaped('2x minecraft:lead', [
    'SS ',
    'SR ',
    '  S'
  ], {
    S: 'minecraft:string',
    R: '#forge:rods/iron'
  })

})