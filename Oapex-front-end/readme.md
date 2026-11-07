# Goals of front end
Send a request to the backend for whatever it needs: 
- Query openrouter via python function for all available audio endpoints, including the json of what parameters they can take.
- Dynamically allow controls to be available based on parameters, for example, a div is empty for when the model doesn't support cloning, but if it does, then fill the div with a file chooser.
- Submit multi line response to the python backend to receive back the audio object
- Play and download audio to the computer
- error handling for when back end reports no models available  on data controls, zdr, etc
- Voices should come up in a combo box, or if a file is selected, that should be used instead, not both passed. 

Nothing  should  ever commit to disc unless the user downloads it specifically. 