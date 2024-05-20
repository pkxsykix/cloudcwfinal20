import json
import random

def lambda_handler(event, context):
    mean = event['mean']
    std = event['std']
    shots = event['shots']
    
    var95_list = []
    var99_list = []
    
    for _ in range(50):  # Run 50 simulations
        simulated = [random.gauss(mean, std) for _ in range(shots)]
        simulated.sort(reverse=True)
        
        var95 = simulated[int(len(simulated) * 0.95)]
        var99 = simulated[int(len(simulated) * 0.99)]
        
        var95_list.append(var95)
        var99_list.append(var99)
    
    result = {
        'var95': var95_list,
        'var99': var99_list
    }
    
    return {
        'statusCode': 200,
        'body': json.dumps(result)
    }
